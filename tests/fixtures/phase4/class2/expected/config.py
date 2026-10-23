class ServerConfig:
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
        self.max_connections = 100
        self.timeout = 30
        self.enable_tls = True
