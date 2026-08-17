"""Request/response models for the OpenAI chat completions API.

Deliberately permissive: clients send plenty of parameters BizChat has no notion of
(`temperature`, `top_p`, `seed`, ...). Rejecting them would break the clients for no
gain, so unknown fields are accepted and ignored.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FunctionCall(BaseModel):
    name: str
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    #: Either a plain string or the multi-part content array.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """Flatten the content to plain text, dropping non-text parts."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts = [
                part.get("text", "")
                for part in self.content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p)
        return ""


class FunctionDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "function"
    function: FunctionDef


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "m365-copilot"
    messages: list[ChatMessage]
    stream: bool = False
    tools: list[ToolDef] | None = None
    tool_choice: Any | None = None
    #: Accepted and ignored — BizChat exposes no sampling controls.
    temperature: float | None = None
    max_tokens: int | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class ChatCompletion(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChunkChoice]


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "microsoft"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


def estimate_tokens(text: str) -> int:
    """Rough token estimate.

    BizChat reports no token counts, but clients read `usage` for budgeting and
    display, so a transparent approximation beats hard-coded zeros. ~4 chars per
    token is the usual English rule of thumb.
    """
    return max(1, len(text) // 4) if text else 0
