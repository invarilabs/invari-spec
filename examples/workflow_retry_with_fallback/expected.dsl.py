workflow("valid_retry_with_fallback")

entity("task", Record(
    status=Enum("ready", "attempting", "succeeded", "failed", "fallback"),
    retry_count=Int,
    max_retries=Int,
    attempt_succeeds=Bool,
))

init(
    Eq(Field("task", "status"), "ready"),
    Eq(Field("task", "retry_count"), 0),
    Eq(Field("task", "max_retries"), 3),
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
    "attempt_fails_can_retry",
    requires=[
        Eq(Field("task", "status"), "attempting"),
        Not(Field("task", "attempt_succeeds")),
        Lt(Field("task", "retry_count"), Field("task", "max_retries")),
    ],
    changes=[
        SetField("task", "retry_count", Add(Field("task", "retry_count"), 1)),
        SetField("task", "status", "failed"),
    ],
)

action(
    "retry_attempt",
    requires=[
        Eq(Field("task", "status"), "failed"),
        Lt(Field("task", "retry_count"), Field("task", "max_retries")),
    ],
    changes=[
        SetField("task", "status", "attempting"),
    ],
)

action(
    "attempt_fails_use_fallback",
    requires=[
        Eq(Field("task", "status"), "attempting"),
        Not(Field("task", "attempt_succeeds")),
        Ge(Field("task", "retry_count"), Field("task", "max_retries")),
    ],
    changes=[
        SetField("task", "status", "fallback"),
    ],
)

invariant(
    "retry_count_never_exceeds_max_retries",
    Le(Field("task", "retry_count"), Field("task", "max_retries")),
)

obligation(
    "workflow_eventually_finishes",
    trigger=Eq(Field("task", "status"), "ready"),
    must_eventually=Or(
        Eq(Field("task", "status"), "succeeded"),
        Eq(Field("task", "status"), "fallback"),
    ),
)
