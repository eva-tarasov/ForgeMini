# TaskCreate обрезает Unicode White_Space. Python str.strip() по умолчанию
# также обрезает четыре управляющих разделителя U+001C–U+001F.
_TASK_WHITESPACE = (
    " \t\n\r\v\f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def normalize_task_input(goal: str, acceptance_criteria: list[str]) -> tuple[str, tuple[str, ...]]:
    """Проверить содержимое новой Task и вернуть неизменяемый результат."""
    normalized_goal = _normalize_text(goal, 500, "invalid_task_goal")
    if not isinstance(acceptance_criteria, list) or not 1 <= len(acceptance_criteria) <= 10:
        raise ValueError("invalid_task_acceptance_criteria")
    criteria = tuple(
        _normalize_text(criterion, 300, "invalid_task_acceptance_criterion")
        for criterion in acceptance_criteria
    )
    return normalized_goal, criteria


def _normalize_text(value: str, maximum: int, error_code: str) -> str:
    if isinstance(value, str):
        normalized = value.strip(_TASK_WHITESPACE)
        if 1 <= len(normalized) <= maximum:
            return normalized
    raise ValueError(error_code)
