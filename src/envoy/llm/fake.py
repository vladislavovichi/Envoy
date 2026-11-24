from collections.abc import Callable, Sequence

from pydantic import BaseModel, ValidationError

from envoy.llm.ports import LanguageModelError, StructuredRequest

type Matcher = Callable[[str], bool]


class ScriptedLanguageModel:
    def __init__(self, script: Sequence[tuple[Matcher, BaseModel]] = ()) -> None:
        self._script = list(script)
        self.requests: list[StructuredRequest[BaseModel]] = []

    def add(self, matcher: Matcher, reply: BaseModel) -> None:
        self._script.append((matcher, reply))

    async def complete[T: BaseModel](self, request: StructuredRequest[T]) -> T:
        self.requests.append(request)
        for matcher, reply in self._script:
            if matcher(request.data):
                try:
                    return request.schema.model_validate(reply.model_dump(mode="json"))
                except ValidationError:
                    raise LanguageModelError("Scripted reply does not match schema") from None
        raise LanguageModelError("No scripted reply for request")
