class PaymentProcessor:
    def process(self, amount: int):
        fee = amount * 0.02
        return amount - fee
