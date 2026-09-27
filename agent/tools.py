"""Tool schema exposed to the discovery LLM.

Deliberately NOT raw Playwright actions (no CSS selectors, no coordinates).
The model only ever refers to elements by the opaque `element_key` from the
latest observation (see core.surface.Observation) -- the grounding /
locator-robustness work happens in core.surface.PlaywrightSurface, not in
the model's head. This keeps the model's job to "what should happen next,"
and keeps "how do I reliably find that control" as an engineering
concern with a real fallback chain, which is what actually has to survive
replay a month later.

type_env exists so credentials never pass through the model's context at
all: the model names an environment-variable, and the harness resolves and
types the real value directly into the page. This is the concrete answer
to "never persist secrets... into artifacts" extended one step further:
secrets never even reach the LLM transcript, not just the saved artifact.
"""

TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate the browser to an absolute URL within the allowed target.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an actionable element by its key from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "reason": {"type": "string", "description": "Why you're clicking this."},
            },
            "required": ["element_key", "reason"],
        },
    },
    {
        "name": "type_literal",
        "description": "Type a fixed literal string into a field (use for values that are not "
                        "one of the run's input parameters, e.g. a constant you observed on screen).",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_key", "text", "reason"],
        },
    },
    {
        "name": "type_param",
        "description": "Type the value of one of this run's named input parameters into a field. "
                        "Always prefer this over type_literal when the value came from the stated "
                        "input parameters, so the recorded capability is reusable with different values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "param_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_key", "param_name", "reason"],
        },
    },
    {
        "name": "type_env",
        "description": "Type a secret value from a named environment variable (e.g. service "
                        "credentials) into a field. You will never see the actual value -- name "
                        "the variable and the harness fills it in directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "env_var": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_key", "env_var", "reason"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a <select> dropdown by its option value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_key", "value", "reason"],
        },
    },
    {
        "name": "extract",
        "description": "Read the visible text of an element and store it under a named output "
                        "for the final capability result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_key": {"type": "string"},
                "output_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_key", "output_name", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the goal reached. checkpoint_text must be a short, exact substring "
                        "of text currently visible on the page that proves the goal state was reached "
                        "(e.g. 'Sub-Account Created'). This becomes the capability's success checkpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_text": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["checkpoint_text", "summary"],
        },
    },
    {
        "name": "give_up",
        "description": "Call this if you are stuck and cannot safely proceed (e.g. an action is "
                        "outside policy, the page is in an unexpected state you don't understand, "
                        "or you've tried reasonable alternatives without progress). This escalates "
                        "to a human rather than guessing.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
