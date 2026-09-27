"""Turn a role's model spec into a chat model.

`CARDINAL_MODEL_PROVIDER=module:callable` replaces model resolution for every agent call. It is how
an outside harness substitutes scripted models; the callable receives the stage context and must
return a chat model. Cardinal ships no scripted models of its own.
"""

import importlib
import os

from langchain.chat_models import init_chat_model


def resolve(spec: str, context: dict, timeout: int):
    hook = os.environ.get("CARDINAL_MODEL_PROVIDER")
    if hook:
        module_name, _, attribute = hook.partition(":")
        factory = getattr(importlib.import_module(module_name), attribute)
        model = factory(context)
        if model is None:
            raise ValueError(f"{hook} returned no model for stage {context.get('stage')}")
        return model
    return init_chat_model(spec, timeout=timeout, max_retries=2)
