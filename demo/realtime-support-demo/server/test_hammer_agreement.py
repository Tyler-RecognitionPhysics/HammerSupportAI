"""Tests for Hammer Drive agreement pricing."""

from __future__ import annotations

import unittest

from hammer_agreement import (
    build_agreement_email,
    build_agreement_email_html,
    dealership_display_name,
    is_facebook_aia_signup,
    is_hammer_connect_signup,
    is_hammer_drive_signup,
    is_marketposter_signup,
    marketposter_monthly_for_users,
    resolve_agreement_pricing,
    resolve_facebook_aia_pricing,
    resolve_hammer_connect_pricing,
    resolve_hammer_drive_pricing,
    resolve_marketposter_pricing,
)


class HammerAgreementTests(unittest.TestCase):
    def test_lot_bands_usd(self) -> None:
        p25 = resolve_hammer_drive_pricing("Hammer Drive", "25", currency="USD")
        assert p25 is not None
        self.assertEqual(p25["subscriptionMonthlyAmount"], "299")
        self.assertEqual(p25["currency"], "USD")

        p45 = resolve_hammer_drive_pricing("Hammer Drive 31-60", "45", currency="USD")
        assert p45 is not None
        self.assertEqual(p45["subscriptionMonthlyAmount"], "399")

        p70 = resolve_hammer_drive_pricing("Hammer Drive", "70", currency="USD")
        assert p70 is not None
        self.assertEqual(p70["subscriptionMonthlyAmount"], "599")

    def test_lot_bands_cad(self) -> None:
        p25 = resolve_hammer_drive_pricing("Hammer Drive", "25", currency="CAD")
        assert p25 is not None
        self.assertEqual(p25["subscriptionMonthlyAmount"], "299")
        self.assertEqual(p25["currency"], "CAD")
        self.assertIn("CAD", p25["subscriptionMonthlyDisplay"])

        p85 = resolve_hammer_drive_pricing("Hammer Drive 81+", "85", currency="CAD")
        assert p85 is not None
        self.assertEqual(p85["subscriptionMonthlyAmount"], "1299")

    def test_dealership_name_from_text(self) -> None:
        self.assertEqual(dealership_display_name("Victory Motorsports"), "Victory Motorsports")

    def test_dealership_name_from_domain(self) -> None:
        self.assertEqual(
            dealership_display_name("https://victory-motors.com"),
            "Victory Motors",
        )

    def test_facebook_aia_detection(self) -> None:
        self.assertTrue(is_facebook_aia_signup("Facebook AIA"))
        self.assertTrue(is_facebook_aia_signup("Facebook AIA $299/mo"))
        self.assertFalse(is_facebook_aia_signup("Hammer Drive 31-60"))
        self.assertFalse(is_hammer_drive_signup("Facebook AIA"))

    def test_facebook_aia_pricing(self) -> None:
        pricing = resolve_facebook_aia_pricing("Facebook AIA", lot_size="45", currency="USD")
        assert pricing is not None
        self.assertEqual(pricing["productLine"], "facebook_aia")
        self.assertEqual(pricing["subscriptionMonthlyAmount"], "299")
        self.assertEqual(pricing["metaAdSpendDailyAmount"], "15")
        self.assertEqual(pricing["serviceDescription"], "Facebook Advertising + AI")
        self.assertEqual(pricing["metaAdSpendDailyDisplay"], "$15/day")

    def test_facebook_aia_agreement_email(self) -> None:
        pricing = resolve_agreement_pricing("Facebook AIA", lot_size="50", currency="USD")
        assert pricing is not None
        html = build_agreement_email_html("Sunrise Ford", pricing)
        self.assertIn("Facebook Advertising + AI", html)
        self.assertIn("<strong>Ad Spend:</strong>", html)
        self.assertIn("$15/day", html)
        self.assertIn("Ad spend for Ads is non-refundable", html)
        self.assertIn("first month of Hammer", html)
        self.assertIn("cancel your account", html)
        self.assertIn("1 day notice", html)

    def test_hammer_connect_pricing_and_email(self) -> None:
        self.assertTrue(is_hammer_connect_signup("Hammer Connect standalone"))
        self.assertFalse(is_marketposter_signup("Hammer Connect"))
        pricing = resolve_hammer_connect_pricing("Hammer Connect", lot_size="45")
        assert pricing is not None
        self.assertEqual(pricing["serviceDescription"], "Hammer Connect")
        self.assertEqual(pricing["subscriptionMonthlyDisplay"], "$99/month")
        html = build_agreement_email_html("Victory Motorsports", pricing)
        self.assertIn("<strong>Your service description:</strong> Hammer Connect", html)
        self.assertIn("<strong>Hammer Connect</strong>", html)
        self.assertIn("<strong>Thank you for choosing Hammer/Hammer Connect!</strong>", html)
        self.assertNotIn("MarketPoster", html)
        self.assertNotIn("Additional Users $50 monthly", html)
        plain = build_agreement_email("Victory Motorsports", pricing)
        self.assertIn("Your service description: Hammer Connect", plain)
        self.assertIn("Welcome to Hammer Connect!", plain)
        self.assertNotIn("Additional Users $50 monthly", plain)
        override = resolve_hammer_connect_pricing("Hammer Connect $199/mo", lot_size="45")
        assert override is not None
        self.assertEqual(override["subscriptionMonthlyAmount"], "99")

    def test_marketposter_pricing_and_email(self) -> None:
        self.assertTrue(is_marketposter_signup("MarketPoster 2 users"))
        self.assertEqual(marketposter_monthly_for_users(2), 249)
        pricing = resolve_marketposter_pricing("MarketPoster", seat_count="2 users")
        assert pricing is not None
        self.assertEqual(pricing["subscriptionMonthlyDisplay"], "$249/month + 2 Users")
        html = build_agreement_email_html("Victory Motorsports", pricing)
        self.assertIn("MarketPoster", html)
        self.assertIn("Facebook Market Place Posting", html)
        self.assertIn("$249/month + 2 Users", html)
        self.assertIn("10-vehicle limit per session", html)
        self.assertIn("Hammer/MarketPoster", html)
        self.assertIn("Additional Users $50 monthly", html)
        self.assertNotIn("trial account", html)
        for tag in (
            "<strong>I approve</strong>",
            "<strong>MarketPoster</strong>",
            "<strong>Your service description:</strong>",
            "<strong>Subscription:</strong>",
            "<strong>Next Payment:</strong>",
            "<strong>$249</strong>",
            "<strong>CANCELLATION POLICY:</strong>",
            "hammertime.com/help",
            "<em>unsubscribe</em>",
            "<strong>1 day notice</strong>",
            "<strong>DATA ACCESS AUTHORIZATION:</strong>",
            "For more information, you can visit the links below",
            "<strong>Thank you for choosing Hammer/MarketPoster!</strong>",
            "&#x1F680;",
        ):
            self.assertIn(tag, html, msg=f"missing {tag}")
        self.assertRegex(
            html,
            r"<strong>Next Payment:</strong> <strong>\d{1,2}/\d{1,2}/\d{2}</strong>",
        )
        plain = build_agreement_email("Victory Motorsports", pricing)
        self.assertIn("Additional Users $50 monthly", plain)
        self.assertIn("🚀", plain)
        blocks = plain.split("\n\n")
        self.assertEqual(blocks[0], "Hello Victory Motorsports,")
        self.assertIn("Please reply", blocks[1])
        self.assertIn("Welcome to MarketPoster", blocks[2])
        self.assertIn("Your service description:", blocks[3])
        self.assertIn("Subscription: $249/month + 2 Users", blocks[3])
        self.assertIn("You will be charged $249 today", blocks[4])
        self.assertEqual(blocks[5], "Additional Users $50 monthly")
        self.assertIn("CANCELLATION POLICY", blocks[6])
        self.assertIn("10-vehicle limit", blocks[7])
        self.assertIn("DATA ACCESS AUTHORIZATION", blocks[8])
        self.assertIn("For more information", blocks[8])
        self.assertIn("We can't wait", blocks[8])
        self.assertIn("🚀", blocks[8])
        self.assertIn("Thank you for choosing Hammer/MarketPoster", blocks[9])
        self.assertEqual(blocks[10], "Cheers,")
        self.assertEqual(blocks[11], "Hannah")
        self.assertIn("Hannah", blocks[12])
        self.assertIn("hannah@hammer-corp.com", blocks[12])

    def test_next_payment_date_format(self) -> None:
        from hammer_agreement import format_next_payment_date

        value = format_next_payment_date()
        self.assertRegex(value, r"^\d{1,2}/\d{1,2}/\d{2}$")

    def test_agreement_email_html_bold_styling(self) -> None:
        pricing = resolve_hammer_drive_pricing("Hammer Drive", "45", currency="USD")
        assert pricing is not None
        html = build_agreement_email_html("Victory Motorsports", pricing)
        self.assertIn("#CC0000", html)
        self.assertIn("HAMMER", html)
        for tag in (
            "<strong>I approve</strong>",
            "<strong>HAMMER</strong>",
            "<strong>Your service description:</strong>",
            "<strong>Subscription:</strong>",
            "<strong>Next Payment:</strong>",
            "<strong>$399 USD today</strong>",
            "<strong>CANCELLATION POLICY:</strong>",
            "<strong>1 day notice</strong>",
            "<em>unsubscribe</em>",
            "<strong>DATA ACCESS AUTHORIZATION:</strong>",
            "<strong>Thank you for choosing Hammer!</strong>",
        ):
            self.assertIn(tag, html)


if __name__ == "__main__":
    unittest.main()

