from __future__ import annotations

import inspect

from pydantic import BaseModel

from chorus import models


def test_every_pydantic_field_has_an_openapi_description() -> None:
    model_types = [
        value
        for value in vars(models).values()
        if inspect.isclass(value) and issubclass(value, BaseModel) and value is not BaseModel
    ]

    missing = [
        f"{model_type.__name__}.{name}"
        for model_type in model_types
        for name, field in model_type.model_fields.items()
        if not field.description
    ]

    assert missing == []
