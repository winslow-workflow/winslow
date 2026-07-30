from textual.validation import Validator, ValidationResult


class FilterSyntaxValidator(Validator):
    def __init__(self, registry):
        super().__init__()
        self.registry = registry

    def validate(self, value) -> ValidationResult:
        if not value.strip():
            return self.success()
        try:
            self.registry.parse(value.strip())
            return self.success()
        except ValueError as e:
            return self.failure(str(e))
