import unittest

from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.parsing.helpers import create_offer_from_merged


class SellerDeduplicationTests(unittest.TestCase):
    def test_create_offer_extracts_and_normalizes_seller_data(self) -> None:
        offer = create_offer_from_merged(
            title="BMW X5 2024",
            url="https://example.com/offers/1",
            domain="example.com",
            model_name="BMW X5",
            merged={
                "price": 5_500_000,
                "seller_name": "Продавец: ООО Тест",
                "seller_profile_url": "/company/test",
            },
            text=(
                "Контактное лицо: ООО Тест. "
                "Телефон: 8 (912) 345-67-89. "
                "E-mail: SALES@Test.ru"
            ),
        )

        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.seller_name, "ООО Тест")
        self.assertEqual(offer.seller_phone, "79123456789")
        self.assertEqual(offer.seller_email, "sales@test.ru")
        self.assertEqual(offer.seller_profile_url, "https://example.com/company/test")




if __name__ == "__main__":
    unittest.main()
