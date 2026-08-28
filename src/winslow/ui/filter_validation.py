from textual.validation import Validator, ValidationResult


class FilterSyntaxValidator(Validator):
    """Mark a filter input invalid when the query does not parse. `parse` is
    any callable that raises ValueError with the parse error, for example
    SessionClient.apply_filter or parse_builtin."""

    def __init__(self, parse):
        super().__init__()
        self.parse = parse

    def validate(self, value) -> ValidationResult:
        if not value.strip():
            return self.success()
        try:
            self.parse(value.strip())
            return self.success()
        except ValueError as e:
            return self.failure(str(e))
