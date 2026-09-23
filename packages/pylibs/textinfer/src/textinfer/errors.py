"""The failures that are the caller's, and so answer 422 rather than 424."""

from __future__ import annotations


class InvalidRequest(ValueError):
    """The caller asked for something this model cannot do: the one failure that is theirs."""


class InputTooLong(InvalidRequest):
    def __init__(self, index: int, tokens: int, limit: int) -> None:
        super().__init__(
            f"`inputs` must have less than {limit} tokens. Given: {tokens} (input {index}); "
            "send `truncate: true` to cut it instead"
        )
        self.index, self.tokens, self.limit = index, tokens, limit
