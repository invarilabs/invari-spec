workflow("missing_fallback")

entity("task", Record(
    status=Enum("ready", "calling_service", "succeeded", "failed", "fallback"),
    service_succeeds=Bool,
    failure_handled=Bool,
))

init(
    Eq(Field("task", "status"), "ready"),
    Eq(Field("task", "service_succeeds"), False),
    Eq(Field("task", "failure_handled"), False),
)

action(
    "call_service",
    requires=[
        Eq(Field("task", "status"), "ready"),
    ],
    changes=[
        SetField("task", "status", "calling_service"),
    ],
)

action(
    "service_call_succeeds",
    requires=[
        Eq(Field("task", "status"), "calling_service"),
        Field("task", "service_succeeds"),
    ],
    changes=[
        SetField("task", "status", "succeeded"),
    ],
)

action(
    "service_call_fails",
    requires=[
        Eq(Field("task", "status"), "calling_service"),
        Not(Field("task", "service_succeeds")),
    ],
    changes=[
        SetField("task", "status", "failed"),
    ],
)

obligation(
    "failed_service_call_is_eventually_handled",
    trigger=Eq(Field("task", "status"), "failed"),
    must_eventually=Or(
        Field("task", "failure_handled"),
        Eq(Field("task", "status"), "fallback"),
    ),
)

completion_requires(
    outcome=Eq(Field("task", "status"), "failed"),
    condition=Field("task", "failure_handled"),
)
