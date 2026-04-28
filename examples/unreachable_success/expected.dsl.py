workflow("unreachable_success")

entity("request", Record(
    status=Enum("draft", "submitted", "reviewed", "succeeded", "rejected"),
    review_complete=Bool,
))

init(
    Eq(Field("request", "status"), "draft"),
    Eq(Field("request", "review_complete"), False),
)

action(
    "submit_request",
    requires=[
        Eq(Field("request", "status"), "draft"),
    ],
    changes=[
        SetField("request", "status", "submitted"),
    ],
)

action(
    "complete_review",
    requires=[
        Eq(Field("request", "status"), "submitted"),
    ],
    changes=[
        SetField("request", "review_complete", True),
        SetField("request", "status", "reviewed"),
    ],
)

action(
    "reject_request",
    requires=[
        Eq(Field("request", "status"), "reviewed"),
    ],
    changes=[
        SetField("request", "status", "rejected"),
    ],
)

invariant(
    "success_requires_completed_review",
    Implies(
        Eq(Field("request", "status"), "succeeded"),
        Field("request", "review_complete"),
    ),
)

obligation(
    "reviewed_request_eventually_succeeds",
    trigger=Field("request", "review_complete"),
    must_eventually=Eq(Field("request", "status"), "succeeded"),
)

completion_requires(
    outcome=Field("request", "review_complete"),
    condition=Eq(Field("request", "status"), "succeeded"),
)
