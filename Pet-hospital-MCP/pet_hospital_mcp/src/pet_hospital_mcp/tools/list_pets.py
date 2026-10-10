"""``list_pets`` 工具：把 ``GET /api/v1/pets`` 暴露给 AI Agent。

严格对应后端契约：

* **请求**：路径固定 ``/api/v1/pets``，只支持 14 个查询参数
  ``q name ownerName ownerPhone species doctor disease status min max
  sortBy order page pageSize``，不额外引入任何适配器私有参数；
* **成功输出**：对应 Go ``store.Result``（即响应信封里的 ``data``）的
  ``items`` / ``total`` / ``page`` / ``pageSize`` / ``totalPages`` / ``totalCost``；
* **兼容真实 JSON**：Go 的 ``records`` / ``charges`` 既可能是 ``null``
  也可能是数组，这里一律声明成 ``list[...] | None``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any, Final, Literal, get_args

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError
from pydantic import BeforeValidator, model_validator

from ..errors import (
    BackendInvalidResponseError,
    ErrorCode,
    InternalError,
    PetHospitalMCPError,
    ValidationError,
)
from ..logging_config import tool_call_scope
from ..rest_client import PetHospitalRestClient
from ._schema import harden_tool_arguments, reject_bool, take_deferred_input_error

__all__ = [
    "TOOL_NAME",
    "LIST_PETS_DESCRIPTION",
    "SPECIES",
    "STATUS",
    "SORT_FIELDS",
    "SORT_ORDERS",
    "QUERY_PARAM_NAMES",
    "Charge",
    "MedicalRecord",
    "Pet",
    "PetListData",
    "ListPetsInput",
    "build_list_pets_tool",
    "register_list_pets",
]

logger = logging.getLogger(__name__)

TOOL_NAME: Final = "list_pets"

#: 后端 ``GET /api/v1/pets?species=`` 允许的种类（见 Go ``model.ValidSpecies``）。
SPECIES: Final = ("犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他")
#: 后端允许的就诊状态（见 Go ``model.ValidStatus``）。
STATUS: Final = ("待就诊", "就诊中", "住院中", "已康复", "慢性病随访")
#: 后端 ``/api/v1/meta`` 公布的排序字段（见 Go ``handleMeta`` 的 ``sortFields``）。
SORT_FIELDS: Final = (
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
)
#: 排序方向。
SORT_ORDERS: Final = ("asc", "desc")
#: 转发的查询参数，顺序即转发顺序，便于测试断言。
QUERY_PARAM_NAMES: Final = (
    "q",
    "name",
    "ownerName",
    "ownerPhone",
    "species",
    "doctor",
    "disease",
    "status",
    "min",
    "max",
    "sortBy",
    "order",
    "page",
    "pageSize",
)

Species = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]  # type: ignore[valid-type]
PetStatus = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]  # type: ignore[valid-type]
SortField = Literal[  # type: ignore[valid-type]
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
SortOrder = Literal["asc", "desc"]  # type: ignore[valid-type]

LIST_PETS_DESCRIPTION: Final = """\
查询宠物医院档案列表（只读）。

用途：按关键词、主人、种类、医生、疾病、就诊状态过滤宠物档案，按指定字段排序，
并分页返回；同时给出过滤结果的总条数、总页数和结果集花费合计。
对应后端接口 `GET /api/v1/pets`。

适用场景：主人来电时按姓名或电话找宠物；查某位医生的在诊病例；统计某类疾病
或某个就诊状态的宠物；找出消费最高的宠物；给 Agent 回答"有多少只猫在住院"
这类需要翻页与总数的问题。

参数（全部可选，未传即不参与过滤，由后端使用默认值）：
- `q`：全文模糊关键词，跨姓名、品种、主人、医生、疾病等字段（空格分词，AND 匹配）。
- `name`：宠物姓名模糊匹配。
- `ownerName`：主人姓名模糊匹配。
- `ownerPhone`：主人电话模糊匹配。
- `species`：种类精确匹配，只能是 犬/猫/兔/鸟/仓鼠/爬宠/其他 之一。
- `doctor`：主治医生姓名模糊匹配。
- `disease`：疾病/主要诊断模糊匹配。
- `status`：就诊状态精确匹配，只能是 待就诊/就诊中/住院中/已康复/慢性病随访 之一。
- `min`：在医院总花费下限（元），非负。
- `max`：在医院总花费上限（元），非负，且不得小于 `min`。
- `sortBy`：排序字段，只能是 id/name/ownerName/species/doctor/disease/status/
  totalCost/visitCount/createdAt/updatedAt 之一；不传时后端按 createdAt 倒序。
- `order`：排序方向，asc 或 desc。
- `page`：页码，从 1 开始。
- `pageSize`：每页条数，1-500，不传时后端默认 20。

返回值：
- `items`：当前页的宠物档案数组，每条包含基本信息、主人信息、医生与疾病、
  就诊状态，以及可能为 null 的 `records`（历史病历）与 `charges`（消费明细）、
  派生字段 `totalCost`（总花费）与 `visitCount`（就诊次数）。
- `total`：过滤后的档案总数（不受分页影响）。
- `page` / `pageSize` / `totalPages`：当前分页位置与总页数。
- `totalCost`：本次过滤结果集中所有档案的总花费合计（元）。

失败时返回统一错误结构 `{"error": {"code", "message", "details"}}`，
错误码见 VALIDATION_ERROR / BACKEND_TIMEOUT / BACKEND_UNAVAILABLE /
BACKEND_API_ERROR / BACKEND_INVALID_RESPONSE / INTERNAL_ERROR。
"""


# ---------------------------------------------------------------------------
# 成功输出模型（对应 Go 的 model.Pet / store.Result）
# ---------------------------------------------------------------------------
class Charge(BaseModel):
    """一次诊疗收费明细（Go ``model.Treatment``）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    item: str = ""
    category: str = ""
    amount: float = 0.0
    doctor: str | None = None
    date: str = ""
    note: str | None = None


class MedicalRecord(BaseModel):
    """一条历史病历（Go ``model.MedicalRecord``）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    visit_date: str = Field(default="", alias="visitDate")
    doctor: str = ""
    diagnosis: str = ""
    symptoms: str | None = None
    treatment: str | None = None
    prescription: list[str] | None = None
    weight_kg: float | None = Field(default=None, alias="weightKg")
    temperature: float | None = None
    follow_up: str | None = Field(default=None, alias="followUp")
    charge: float = 0.0
    created_at: str = Field(default="", alias="createdAt")


class Pet(BaseModel):
    """宠物档案（Go ``model.Pet``）。字段名保持与后端一致，避免二次心智映射。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    species: str
    breed: str | None = None
    gender: str | None = None
    age_months: int | None = Field(default=None, alias="ageMonths")
    color: str | None = None
    chip_no: str | None = Field(default=None, alias="chipNo")

    owner_name: str = Field(default="", alias="ownerName")
    owner_phone: str = Field(default="", alias="ownerPhone")
    owner_addr: str | None = Field(default=None, alias="ownerAddr")

    doctor: str = ""
    disease: str = ""
    status: str = ""
    allergy: str | None = None
    note: str | None = None

    # Go 用 nil slice 序列化成 null，这里必须是 Optional。
    records: list[MedicalRecord] | None = None
    charges: list[Charge] | None = None

    total_cost: float = Field(default=0.0, alias="totalCost")
    visit_count: int = Field(default=0, alias="visitCount")
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")


class PetListData(BaseModel):
    """列表查询结果（Go ``store.Result``，即响应信封的 ``data``）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    items: list[Pet] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = Field(default=20, alias="pageSize")
    total_pages: int = Field(default=0, alias="totalPages")
    total_cost: float = Field(default=0.0, alias="totalCost")


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------
class ListPetsInput(BaseModel):
    """``list_pets`` 的严格输入模型。

    单值约束（枚举、范围）在工具函数的参数注解上已经声明一遍，
    这里再做一次带 ``extra="forbid"`` 与 ``allow_inf_nan=False`` 的整体校验，
    负责跨字段规则（``min <= max``）和兜底的类型 / 数值检查。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    q: str | None = None
    name: str | None = None
    owner_name: str | None = Field(default=None, alias="ownerName")
    owner_phone: str | None = Field(default=None, alias="ownerPhone")
    species: Species | None = None
    doctor: str | None = None
    disease: str | None = None
    status: PetStatus | None = None
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)
    sort_by: SortField | None = Field(default=None, alias="sortBy")
    order: SortOrder | None = None
    page: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, alias="pageSize", ge=1, le=500)

    @model_validator(mode="after")
    def _validate_cost_range(self) -> ListPetsInput:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min 不能大于 max")
        return self

    def to_query(self) -> dict[str, str]:
        """转成后端查询参数；``None`` 表示"不参与过滤"，不写入。"""
        raw: dict[str, Any] = {
            "q": self.q,
            "name": self.name,
            "ownerName": self.owner_name,
            "ownerPhone": self.owner_phone,
            "species": self.species,
            "doctor": self.doctor,
            "disease": self.disease,
            "status": self.status,
            "min": _format_number(self.min),
            "max": _format_number(self.max),
            "sortBy": self.sort_by,
            "order": self.order,
            "page": _format_number(self.page),
            "pageSize": _format_number(self.page_size),
        }
        return {key: raw[key] for key in QUERY_PARAM_NAMES if raw[key] is not None}


def _format_number(value: float | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if value == int(value):
        return str(int(value))
    return repr(value)


# ---------------------------------------------------------------------------
# 工具装配
# ---------------------------------------------------------------------------
def build_list_pets_tool(
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> Callable[..., Any]:
    """返回绑定到 ``client`` 的 ``list_pets`` 协程。

    抽成工厂而不是模块级函数，是为了让每个 :class:`MCPServer` 持有自己的
    REST 客户端（测试里可以各自注入 ``httpx.MockTransport``）。
    """
    log = tool_logger or logger

    async def list_pets(
        q: Annotated[
            str | None, Field(description="全文模糊关键词，跨姓名/品种/主人/医生/疾病等字段")
        ] = None,
        name: Annotated[str | None, Field(description="宠物姓名模糊匹配")] = None,
        ownerName: Annotated[  # noqa: N803 - 与后端查询参数同名，属于契约的一部分
            str | None, Field(description="主人姓名模糊匹配")
        ] = None,
        ownerPhone: Annotated[  # noqa: N803
            str | None, Field(description="主人电话模糊匹配")
        ] = None,
        species: Annotated[
            Species | None,
            Field(description="种类精确匹配：" + "/".join(SPECIES)),
        ] = None,
        doctor: Annotated[str | None, Field(description="主治医生姓名模糊匹配")] = None,
        disease: Annotated[str | None, Field(description="疾病/主要诊断模糊匹配")] = None,
        status: Annotated[
            PetStatus | None,
            Field(description="就诊状态精确匹配：" + "/".join(STATUS)),
        ] = None,
        min: Annotated[  # noqa: A002
            float | None,
            BeforeValidator(reject_bool),
            Field(ge=0, description="在医院总花费下限（元），非负"),
        ] = None,
        max: Annotated[  # noqa: A002
            float | None,
            BeforeValidator(reject_bool),
            Field(ge=0, description="在医院总花费上限（元），不得小于 min"),
        ] = None,
        sortBy: Annotated[  # noqa: N803
            SortField | None,
            Field(description="排序字段：" + "/".join(SORT_FIELDS) + "；不传按 createdAt 倒序"),
        ] = None,
        order: Annotated[SortOrder | None, Field(description="排序方向：asc 或 desc")] = None,
        page: Annotated[
            int | None,
            BeforeValidator(reject_bool),
            Field(ge=1, description="页码，从 1 开始"),
        ] = None,
        pageSize: Annotated[  # noqa: N803
            int | None,
            BeforeValidator(reject_bool),
            Field(ge=1, le=500, description="每页条数，1-500；不传时后端默认 20"),
        ] = None,
    ) -> PetListData:
        """查询宠物医院档案列表（对应后端 ``GET /api/v1/pets``）。"""
        # SDK 的入参模型负责拒绝未知字段，这里取回它暂存的统一错误文案。
        deferred = take_deferred_input_error()
        if deferred is not None:
            raise ToolError(deferred)

        try:
            params = ListPetsInput(
                q=q,
                name=name,
                ownerName=ownerName,
                ownerPhone=ownerPhone,
                species=species,
                doctor=doctor,
                disease=disease,
                status=status,
                min=min,
                max=max,
                sortBy=sortBy,
                order=order,
                page=page,
                pageSize=pageSize,
            ).to_query()
        except PydanticValidationError as exc:
            raise ToolError(
                ValidationError(
                    "工具入参校验失败",
                    details={"errors": _describe_validation_error(exc)},
                ).to_json()
            ) from exc

        with tool_call_scope(log, TOOL_NAME, params) as record:
            try:
                data = await client.list_pets(params)
            except PetHospitalMCPError as exc:
                record.fail(exc.code)
                raise ToolError(exc.to_json()) from exc
            except Exception as exc:  # 兜底：绝不让原始异常文本流向客户端
                record.fail(ErrorCode.INTERNAL_ERROR)
                raise ToolError(
                    InternalError(details={"reason": type(exc).__name__}).to_json()
                ) from exc

            try:
                return PetListData.model_validate(data)
            except PydanticValidationError as exc:
                record.fail(ErrorCode.BACKEND_INVALID_RESPONSE)
                raise ToolError(
                    BackendInvalidResponseError(
                        "宠物医院 REST API 返回的数据结构不符合契约",
                        details={"errors": _describe_validation_error(exc)},
                    ).to_json()
                ) from exc

    return list_pets


def register_list_pets(
    server: MCPServer,
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> None:
    """把 ``list_pets`` 注册到 ``server``。后续阶段的新工具照抄这段即可。"""
    server.add_tool(
        build_list_pets_tool(client, tool_logger=tool_logger),
        name=TOOL_NAME,
        title="查询宠物档案列表",
        description=LIST_PETS_DESCRIPTION,
        annotations=ToolAnnotations(
            title="查询宠物档案列表",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    # SDK 2.x 默认丢弃未知参数，这里收紧为"拒绝"，让入参校验名副其实。
    harden_tool_arguments(server, TOOL_NAME)


def _describe_validation_error(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """把 Pydantic 错误压成"字段 + 原因"，不回显调用方的原始取值。"""
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "(cross-field)",
            "reason": error["type"],
        }
        for error in exc.errors()[:20]
    ]


# 供文档与测试断言使用：确认 Literal 与常量表一致。
assert get_args(Species) == SPECIES
assert get_args(PetStatus) == STATUS
assert get_args(SortField) == SORT_FIELDS
assert get_args(SortOrder) == SORT_ORDERS