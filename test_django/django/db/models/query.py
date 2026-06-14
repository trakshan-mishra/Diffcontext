from django.db.models import sql
class QuerySet:
    def count(self):
        self.query = sql.Query()
        self.query.get_count()
