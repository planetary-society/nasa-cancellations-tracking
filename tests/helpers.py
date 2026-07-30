class FakeTxn:
    """Duck-typed stand-in for usaspending's Transaction.

    The classifier reads these five fields and nothing else, which is what lets
    the decision rules be tested without touching the network.
    """

    def __init__(
        self,
        action_date,
        modification_number="",
        action_type="",
        award_description="",
        federal_action_obligation=None,
    ):
        self.action_date = action_date
        self.modification_number = modification_number
        self.action_type = action_type
        self.award_description = award_description
        self.federal_action_obligation = federal_action_obligation
