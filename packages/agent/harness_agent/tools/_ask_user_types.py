"""ask_user 中断协议的轻量类型，避免类型引用触发完整中间件导入。

从 ``ask_user`` 拆出后，``textual_adapter`` 与应用类型检查可直接引用
``AskUserRequest``，而不会触发 LangChain 中间件栈导入。
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import Field
from typing_extensions import TypedDict


class Choice(TypedDict):
    """多选题的单个候选项。"""

    value: Annotated[str, Field(description="该候选项在界面中显示的标签。")]


class Question(TypedDict):
    """需要展示给用户的单个问题。"""

    question: Annotated[str, Field(description="需要展示给用户的问题文本。")]

    type: Annotated[
        Literal["text", "multiple_choice"],
        Field(
            description=(
                "问题类型：text 表示自由输入，multiple_choice 表示预置候选项。"
            )
        ),
    ]

    choices: NotRequired[
        Annotated[
            list[Choice],
            Field(
                description=(
                    "多选题候选项；界面会自动追加可自由输入的 Other 选项。"
                )
            ),
        ]
    ]

    required: NotRequired[
        Annotated[
            bool,
            Field(
                description="用户是否必须回答；缺省时为 true。"
            ),
        ]
    ]


class AskUserRequest(TypedDict):
    """发起 ask_user interrupt 时发送的请求载荷。"""

    type: Literal["ask_user"]
    """判别标签，固定为 ``ask_user``。"""

    questions: list[Question]
    """需要展示给用户的问题列表。"""

    tool_call_id: str
    """来源工具调用 ID，用于把回答准确路由回原调用。"""


class AskUserAnswered(TypedDict):
    """用户提交回答后的 UI 结果。"""

    type: Literal["answered"]
    """判别标签，固定为 ``answered``。"""

    answers: list[str]
    """用户提供的回答，与问题列表按顺序对应。"""


class AskUserCancelled(TypedDict):
    """用户取消提问后的 UI 结果。"""

    type: Literal["cancelled"]
    """判别标签，固定为 ``cancelled``。"""


AskUserWidgetResult = AskUserAnswered | AskUserCancelled
"""ask_user 组件 Future 结果的判别联合类型。"""
