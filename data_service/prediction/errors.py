class AllMethodsFailed(ValueError):
    """Pickle-safe across ProcessPoolExecutor, retaining each method's reason."""
    def __init__(self, method_errors):
        self.method_errors = method_errors
        super().__init__(method_errors)

    def __str__(self):
        return '；'.join(f"{row['method']['name']}：{row['message']}" for row in self.method_errors)
