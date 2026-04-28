workflow("infinite_retry")

entity("task", Record(
    status=Enum("ready", "attempting", "succeeded", "failed"),
    retry_count=Int,
    attempt_succeeds=Bool,
))

init(
    Eq(Field("task", "status"), "ready"),
    Eq(Field("task", "retry_count"), 0),
    Eq(Field("task", "attempt_succeeds"), False),
)

action(
    "start_attempt",
    requires=[
        Eq(Field("task", "status"), "ready"),
    ],
    changes=[
        SetField("task", "status", "attempting"),
    ],
)

action(
    "attempt_succeeds",
    requires=[
        Eq(Field("task", "status"), "attempting"),
        Field("task", "attempt_succeeds"),
    ],
    changes=[
        SetField("task", "status", "succeeded"),
    ],
)

action(
    "attempt_fails",
    requires=[
        Eq(Field("task", "status"), "attempting"),
        Not(Field("task", "attempt_succeeds")),
    ],
    changes=[
        SetField("task", "retry_count", Add(Field("task", "retry_count"), 1)),
        SetField("task", "status", "failed"),
    ],
)

action(
    "retry_after_failure",
    requires=[
        Eq(Field("task", "status"), "failed"),
    ],
    changes=[
        SetField("task", "status", "attempting"),
    ],
)

invariant(
    "retry_count_never_negative",
    Ge(Field("task", "retry_count"), 0),
)

obligation(
    "workflow_eventually_succeeds",
    trigger=Eq(Field("task", "status"), "ready"),
    must_eventually=Eq(Field("task", "status"), "succeeded"),
)
