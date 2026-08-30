class QueryBuilder:
    def __init__(self):
        self.conditions = []
        self.args = []

    def where(self, sql: str, *args):
        start = len(self.args) + 1

        sql = sql.format(*[f"${start + i}" for i in range(len(args))])

        self.conditions.append(sql)
        self.args.extend(args)

    def build_where(self):
        return " AND ".join(self.conditions)
