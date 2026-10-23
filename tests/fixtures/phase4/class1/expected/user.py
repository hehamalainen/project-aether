class User:
    def __init__(self):
        self.id = 1
        self.name = "Alice"
        self.role = "user"

    def get_role(self):
        return self.role

    def is_admin(self):
        return self.role == "admin"
