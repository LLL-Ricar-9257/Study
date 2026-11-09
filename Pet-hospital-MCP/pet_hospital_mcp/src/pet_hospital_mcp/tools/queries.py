"""只读查询工具：``get_pet`` / ``get_pet_summary`` / ``get_hospital_stats``。

三个工具与 :mod:`._schema` / :mod:`.list_pets` 共用同一套装配规则：

* 入参模型 ``extra="forbid"`` + 范围约束，未知字段一律拒绝；
* 成功输出用 Pydantic 模型，字段名与 Go 的 JSON 标签一一对应（camelCase 走 alias），
  ``extra="ignore"`` 允许后端加字段而不破坏契约；
* 失败统一走 ``{"error": {code, message, details}}``，绝不把堆栈、httpx 文本或
  上游 HTML 抛给客户端；
* 每次调用都输出一条 ``tool_call`` 结构化日志（含耗时与脱敏参数）。

三个工具全部只读，不修改后端任何数据。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any, Final

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ..errors import (
    BackendInvalidResponseError,
    ErrorCode,
    InternalError,
    PetHospitalMCPError,
)
from ..logging_config import tool_call_scope
from ..rest_client import PetHospitalRestClient
from ._schema import harden_tool_arguments, take_deferred_input_error
from .list_pets import Pet

__all__ = [
    "GET_HOSPITAL_STATS_DESCRIPTION",
    "GET_HOSPITAL_STATS_TOOL",
    "GET_PET_DESCRIPTION",
    "GET_PET_SUMMARY_DESCRIPTION",
    "GET_PET_SUMMARY_TOOL",
    "GET_PET_TOOL",
    "PET_ID_PATTERN",
    "HospitalStatsData",
    "PetSummaryData",
    "build_get_hospital_stats_tool",
    "build_get_pet_summary_tool",
    "build_get_pet_tool",
    "register_query_tools",
]

logger = logging.getLogger(__name__)

GET_PET_TOOL: Final = "get_pet"
GET_PET_SUMMARY_TOOL: Final = "get_pet_summary"
GET_HOSPITAL_STATS_TOOL: Final = "get_hospital_stats"

PET_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
"""后端主键形如 ``PET-000001``；这里只放行字母数字与连字符，杜绝路径穿越。"""

MAX_SUMMARY_HISTORY: Final = 4_000
"""``historyText`` 的长度上限：整段病历是自由文本，避免它把工具结果撑爆。"""


# ---------------------------------------------------------------------------
# 成功输出模型（字段严格对应 Go 的 JSON 标签）
# ---------------------------------------------------------------------------
class PetSummaryData(BaseModel):
    """单只宠物的费用与就诊汇总（Go ``handleSummary``）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    name: str = ""
    species: str = ""
    owner_name: str = Field(default="", alias="ownerName")
    owner_phone: str = Field(default="", alias="ownerPhone")
    doctor: str = ""
    disease: str = ""
    status: str = ""

    total_cost: float = Field(default=0.0, alias="totalCost")
    visit_count: int = Field(default=0, alias="visitCount")
    charge_count: int = Field(default=0, alias="chargeCount")
    cost_by_category: dict[str, float] = Field(default_factory=dict, alias="costByCategory")
    cost_by_doctor: dict[str, float] = Field(default_factory=dict, alias="costByDoctor")
    max_single_charge: float = Field(default=0.0, alias="maxSingleCharge")
    avg_cost_per_visit: float = Field(default=0.0, alias="avgCostPerVisit")
    first_visit: str = Field(default="", alias="firstVisit")
    last_visit: str = Field(default="", alias="lastVisit")
    history_text: str = Field(default="", alias="historyText")


class HospitalStatsData(BaseModel):
    """医院经营统计（Go ``store.Stats``）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    total_pets: int = Field(default=0, alias="totalPets")
    total_records: int = Field(default=0, alias="totalRecords")
    total_charges: int = Field(default=0, alias="totalCharges")
    total_revenue: float = Field(default=0.0, alias="totalRevenue")
    average_cost: float = Field(default=0.0, alias="averageCost")
    max_cost: float = Field(default=0.0, alias="maxCost")
    by_species: dict[str, int] = Field(default_factory=dict, alias="bySpecies")
    by_status: dict[str, int] = Field(default_factory=dict, alias="byStatus")
    by_doctor: dict[str, int] = Field(default_factory=dict, alias="byDoctor")
    revenue_by_doctor: dict[str, float] = Field(default_factory=dict, alias="revenueByDoctor")
    top_spenders: list[Pet] = Field(default_factory=list, alias="topSpenders")
    log_garbage_pct: float = Field(default=0.0, alias="logGarbagePct")


# ---------------------------------------------------------------------------
# 工具描述（面向模型）
# ---------------------------------------------------------------------------
GET_PET_DESCRIPTION: Final = """\
按档案编号读取单只宠物的完整宠物医院档案（只读）。

用途：已知宠物编号（形如 `PET-000001`）时，一次拿到该宠物的基本信息、主人联系方式、
主治医生与疾病、就诊状态，以及全部历史病历 `records` 与消费明细 `charges`。
对应后端接口 `GET /api/v1/pets/{id}`。

适用场景：用户报出档案编号要"看这只宠物的完整病历"；`list_pets` 查到候选后需要看细节；
核对某次就诊的诊断、用药、体重、体温与单次收费。

参数：
- `pet_id`：宠物档案编号，例如 `PET-000001`；只允许字母、数字、连字符和下划线，
  不能包含 `/` 等路径字符。

返回值：宠物档案对象，字段与 `list_pets` 的 `items[]` 完全一致
（`records` / `charges` 可能为 null，也可能是数组）。

失败时返回统一错误结构 `{"error": {"code", "message", "details"}}`；
编号不存在时是 BACKEND_API_ERROR，编号格式非法时是 VALIDATION_ERROR。
"""

GET_PET_SUMMARY_DESCRIPTION: Final = """\
按档案编号读取单只宠物的费用与就诊汇总（只读，不返回逐条明细）。

用途：回答"这只宠物一共花了多少钱""钱花在哪些项目上""哪位医生贡献的收入最高"
"平均每次就诊花费多少""第一次/最后一次就诊是哪天"。对应后端接口
`GET /api/v1/pets/{id}/summary`。

适用场景：用户关心总账而不是逐条流水；需要按费用分类或按医生对比；需要在回答里
给出就诊时间跨度。需要逐条病历或收费明细时改用 `get_pet`。

参数：
- `pet_id`：宠物档案编号，例如 `PET-000001`；只允许字母、数字、连字符和下划线。

返回值：`totalCost` / `visitCount` / `chargeCount` / `costByCategory` /
`costByDoctor` / `maxSingleCharge` / `avgCostPerVisit` / `firstVisit` /
`lastVisit`，以及后端生成的自然语言病史摘要 `historyText`（超过 4000 字符会截断并标注）。

失败时返回统一错误结构 `{"error": {"code", "message", "details"}}`。
"""

GET_HOSPITAL_STATS_DESCRIPTION: Final = """\
读取整家宠物医院的经营统计（只读）。

用途：回答"医院一共有多少只宠物在档""总收入多少""平均每只花了多少钱""哪类宠物最多"
"各就诊状态各有多少""哪位医生接诊最多、创收最高""消费榜前几名是谁"。
对应后端接口 `GET /api/v1/stats?top=N`。

适用场景：用户询问全院口径的汇总数字、排行榜或分布；需要先了解数据规模再决定
是否继续用 `list_pets` 做明细查询。

参数：
- `top`：消费排行榜返回的条数，1-20，默认 5。榜单每一项都是完整的宠物档案。

返回值：`totalPets` / `totalRecords` / `totalCharges` / `totalRevenue` /
`averageCost` / `maxCost`、`bySpecies` / `byStatus` / `byDoctor` 三个分布映射、
`revenueByDoctor` 医生创收映射、`topSpenders` 消费榜数组、`logGarbagePct` 存储碎片率。

失败时返回统一错误结构 `{"error": {"code", "message", "details"}}`。
"""


# ---------------------------------------------------------------------------
# 共用执行器
# ---------------------------------------------------------------------------
async def _execute(
    log: logging.Logger,
    tool_name: str,
    params: dict[str, Any],
    pending: Any,
    model: type[BaseModel],
) -> Any:
    """执行一次后端调用并把结果校验成 ``model``；异常一律转统一错误信封。"""
    with tool_call_scope(log, tool_name, params) as record:
        try:
            data = await pending
        except PetHospitalMCPError as exc:
            record.fail(exc.code)
            raise ToolError(exc.to_json()) from exc
        except Exception as exc:  # 兜底：绝不让原始异常文本流向客户端
            record.fail(ErrorCode.INTERNAL_ERROR)
            raise ToolError(
                InternalError(details={"reason": type(exc).__name__}).to_json()
            ) from exc

        try:
            return model.model_validate(data)
        except PydanticValidationError as exc:
            record.fail(ErrorCode.BACKEND_INVALID_RESPONSE)
            raise ToolError(
                BackendInvalidResponseError(
                    "宠物医院 REST API 返回的数据结构不符合契约",
                    details={"errors": _describe_validation_error(exc)},
                ).to_json()
            ) from exc


def _describe_validation_error(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """压成"字段 + 原因"，不回显调用方取值。"""
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "(root)",
            "reason": error["type"],
        }
        for error in exc.errors()[:20]
    ]


def _take_deferred() -> None:
    """SDK 的入参模型拒绝未知字段时，取回它暂存的统一错误文案。"""
    deferred = take_deferred_input_error()
    if deferred is not None:
        raise ToolError(deferred)


# ---------------------------------------------------------------------------
# 工具装配
# ---------------------------------------------------------------------------
def build_get_pet_tool(
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> Callable[..., Any]:
    """返回绑定到 ``client`` 的 ``get_pet`` 协程。"""
    log = tool_logger or logger

    async def get_pet(
        pet_id: Annotated[
            str,
            Field(
                pattern=PET_ID_PATTERN,
                description="宠物档案编号，例如 PET-000001；只允许字母、数字、连字符和下划线",
            ),
        ],
    ) -> Pet:
        """按档案编号读取单只宠物的完整档案（含病历与收费明细）。"""
        _take_deferred()
        return await _execute(
            log,
            GET_PET_TOOL,
            {"pet_id": pet_id},
            client.get_pet(pet_id),
            Pet,
        )

    return get_pet


def build_get_pet_summary_tool(
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> Callable[..., Any]:
    """返回绑定到 ``client`` 的 ``get_pet_summary`` 协程。"""
    log = tool_logger or logger

    async def get_pet_summary(
        pet_id: Annotated[
            str,
            Field(
                pattern=PET_ID_PATTERN,
                description="宠物档案编号，例如 PET-000001；只允许字母、数字、连字符和下划线",
            ),
        ],
    ) -> PetSummaryData:
        """读取单只宠物的费用与就诊汇总。"""
        _take_deferred()
        summary = await _execute(
            log,
            GET_PET_SUMMARY_TOOL,
            {"pet_id": pet_id},
            client.get_pet_summary(pet_id),
            PetSummaryData,
        )
        # 病史是自由文本，按上限截断，避免单个工具结果占满上下文。
        if len(summary.history_text) > MAX_SUMMARY_HISTORY:
            summary = summary.model_copy(
                update={
                    "history_text": summary.history_text[:MAX_SUMMARY_HISTORY]
                    + f"\n…（病史已截断，原始长度 {len(summary.history_text)} 字符）"
                }
            )
        return summary

    return get_pet_summary


def build_get_hospital_stats_tool(
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> Callable[..., Any]:
    """返回绑定到 ``client`` 的 ``get_hospital_stats`` 协程。"""
    log = tool_logger or logger

    async def get_hospital_stats(
        top: Annotated[
            int,
            Field(ge=1, le=20, description="消费排行榜返回条数，1-20，默认 5"),
        ] = 5,
    ) -> HospitalStatsData:
        """读取整家宠物医院的经营统计与消费排行榜。"""
        _take_deferred()
        return await _execute(
            log,
            GET_HOSPITAL_STATS_TOOL,
            {"top": top},
            client.get_stats(top),
            HospitalStatsData,
        )

    return get_hospital_stats


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def register_query_tools(
    server: MCPServer,
    client: PetHospitalRestClient,
    *,
    tool_logger: logging.Logger | None = None,
) -> None:
    """把三个只读查询工具注册到 ``server``。"""
    registrations: tuple[tuple[str, str, str, Callable[..., Any]], ...] = (
        (
            GET_PET_TOOL,
            "按编号读取宠物档案",
            GET_PET_DESCRIPTION,
            build_get_pet_tool,
        ),
        (
            GET_PET_SUMMARY_TOOL,
            "读取单只宠物的费用与就诊汇总",
            GET_PET_SUMMARY_DESCRIPTION,
            build_get_pet_summary_tool,
        ),
        (
            GET_HOSPITAL_STATS_TOOL,
            "读取医院经营统计",
            GET_HOSPITAL_STATS_DESCRIPTION,
            build_get_hospital_stats_tool,
        ),
    )

    for name, title, description, factory in registrations:
        server.add_tool(
            factory(client, tool_logger=tool_logger),  # type: ignore[operator]
            name=name,
            title=title,
            description=description,
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        harden_tool_arguments(server, name)
