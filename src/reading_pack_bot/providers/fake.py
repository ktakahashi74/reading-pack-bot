"""Network-free deterministic provider."""

from __future__ import annotations

from ..models import GenerationRequest, GenerationResult, Usage


class FakeProvider:
    def __init__(self, response_text: str | None = None) -> None:
        self.response_text = response_text
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        text = self.response_text
        if text is None:
            text = f"FAKE[{request.pack.sha256[:12]}]: {request.current_question}"
        return GenerationResult(
            text=text,
            model="fake",
            response_id="fake-response",
            usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        )
