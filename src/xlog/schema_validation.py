import re


class SchemaValidationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _display_path(parts):
    value = "$"
    for part in parts:
        if isinstance(part, int):
            value += "[%d]" % part
        else:
            value += "." + str(part)
    return value


def _resolve_ref(root_schema, reference):
    if not reference.startswith("#/"):
        raise ValueError("only local JSON Schema references are supported: %s" % reference)
    value = root_schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        value = value[part]
    return value


def _matches_type(value, expected):
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _validate(value, schema, root_schema, path, errors):
    if "$ref" in schema:
        _validate(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path, errors)
        return

    if "anyOf" in schema:
        alternatives = []
        for branch in schema["anyOf"]:
            branch_errors = []
            _validate(value, branch, root_schema, path, branch_errors)
            alternatives.append(branch_errors)
        if not any(not branch_errors for branch_errors in alternatives):
            errors.append("%s: does not match anyOf" % _display_path(path))
        return

    expected_types = schema.get("type")
    if expected_types is not None:
        if not isinstance(expected_types, list):
            expected_types = [expected_types]
        if not any(_matches_type(value, expected) for expected in expected_types):
            errors.append(
                "%s: expected type %s, got %s"
                % (_display_path(path), "|".join(expected_types), type(value).__name__)
            )
            return

    if "const" in schema and value != schema["const"]:
        errors.append("%s: expected constant %r" % (_display_path(path), schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: value %r is not in enum" % (_display_path(path), value))

    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append("%s: missing required property %s" % (_display_path(path), name))
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], root_schema, path + (name,), errors)
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected property %s" % (_display_path(path), name))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: expected at least %d items" % (_display_path(path), schema["minItems"]))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, root_schema, path + (index,), errors)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: expected at least %d characters" % (_display_path(path), schema["minLength"]))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append("%s: value does not match %s" % (_display_path(path), schema["pattern"]))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: value is less than %s" % (_display_path(path), schema["minimum"]))


def validation_errors(instance, schema):
    """Validate the JSON Schema subset used by xlog without external packages."""
    errors = []
    _validate(instance, schema, schema, (), errors)
    return errors


def validate_instance(instance, schema):
    errors = validation_errors(instance, schema)
    if errors:
        raise SchemaValidationError(errors)
    return True
