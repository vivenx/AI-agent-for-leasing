import unittest

from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.parsing.helpers import create_offer_from_merged, deduplicate_offers_by_seller


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

    def test_deduplicate_offers_by_seller_keeps_one_representative_offer(self) -> None:
        offers = [
            LeasingOffer(
                title="BMW X5 2023",
                url="https://avito.ru/item-1",
                source="avito.ru",
                price=4_800_000,
                price_str="4 800 000 ₽",
                model="BMW X5",
                year=2023,
                seller_name="ООО Прайм",
                seller_phone="79120000000",
            ),
            LeasingOffer(
                title="BMW X5 2024 M Sport",
                url="https://dealer.example/item-2",
                source="dealer.example",
                price=5_000_000,
                price_str="5 000 000 ₽",
                model="BMW X5",
                year=2024,
                condition="новый",
                location="Екатеринбург",
                seller_name="ООО Прайм",
                seller_phone="79120000000",
                specs={"drive": "4x4", "engine": "3.0"},
            ),
            LeasingOffer(
                title="BMW X5 2024",
                url="https://avito.ru/item-3",
                source="avito.ru",
                price=5_200_000,
                price_str="5 200 000 ₽",
                model="BMW X5",
                year=2024,
                seller_name="ООО Второй продавец",
                seller_phone="79129999999",
            ),
        ]

        filtered = deduplicate_offers_by_seller(offers)

        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].url, "https://dealer.example/item-2")
        self.assertEqual(filtered[1].url, "https://avito.ru/item-3")

    def test_deduplicate_offers_by_seller_does_not_drop_offers_without_seller_data(self) -> None:
        offers = [
            LeasingOffer(
                title="Offer 1",
                url="https://example.com/1",
                source="example.com",
                price=100,
                price_str="100 ₽",
            ),
            LeasingOffer(
                title="Offer 2",
                url="https://example.com/2",
                source="example.com",
                price=200,
                price_str="200 ₽",
            ),
        ]

        filtered = deduplicate_offers_by_seller(offers)

        self.assertEqual(len(filtered), 2)
        self.assertEqual(
            [offer.url for offer in filtered],
            ["https://example.com/1", "https://example.com/2"],
        )


if __name__ == "__main__":
    unittest.main()
