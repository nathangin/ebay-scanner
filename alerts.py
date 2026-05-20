import time
from datetime import timedelta

try:
    from discord_webhook import DiscordEmbed, DiscordWebhook
except ImportError:
    DiscordEmbed = None
    DiscordWebhook = None

try:
    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    Console = None
    Live = None
    Align = None
    Panel = None
    Table = None
    Text = None


class Dashboard:
    def __init__(self):
        self.console = Console() if Console else None
        self.start_time = time.time()
        self.stats = {"scanned": 0, "deals": 0}
        self.deals = []
        self.last_listings = []
        self.live = Live(self.render(), console=self.console, refresh_per_second=2) if self.console and Live else None

    def start(self):
        if self.live:
            self.live.start()

    def stop(self):
        if self.live:
            self.live.stop()

    def update_stats(self, scanned, deals):
        self.stats["scanned"] = scanned
        self.stats["deals"] = deals
        self.refresh()

    def add_deal(self, item):
        self.deals.insert(0, item)
        self.deals = self.deals[:12]
        self.refresh()

    def log_listing(self, item):
        self.last_listings.insert(0, item)
        self.last_listings = self.last_listings[:5]
        self.refresh()

    def _render_stats_panel(self):
        elapsed = int(time.time() - self.start_time)
        uptime = str(timedelta(seconds=elapsed)).split(".")[0]
        table = Table.grid(expand=True)
        table.add_column(justify="center")
        table.add_column(justify="center")
        table.add_column(justify="center")
        table.add_row(
            f"[bold green]Scanned[/]: {self.stats['scanned']}",
            f"[bold yellow]Deals[/]: {self.stats['deals']}",
            f"[bold cyan]Uptime[/]: {uptime}",
        )
        return Panel(table, title="Scanner Stats", border_style="bright_blue")

    def _render_deals_table(self):
        table = Table(expand=True, show_header=True, header_style="bold magenta")
        table.add_column("Score", style="bold")
        table.add_column("Level")
        table.add_column("Card", overflow="fold")
        table.add_column("Prices", overflow="fold")

        for deal in self.deals:
            score = deal.get("score")
            level = deal.get("level")
            card = deal.get("card_name") or deal.get("title")
            prices = deal.get("summary")
            row_style = "bold red" if score >= 80 else "yellow" if score >= 60 else "green"
            table.add_row(str(int(score)), level, card, prices, style=row_style)

        return Panel(table, title="Recent Deals", border_style="red")

    def _render_last_table(self):
        table = Table(expand=True, show_header=True, header_style="bold green")
        table.add_column("Card", overflow="fold")
        table.add_column("Result")
        for item in self.last_listings:
            table.add_row(item.get("card_name", "Unknown"), item.get("result", "-"))
        return Panel(table, title="Last Checked", border_style="green")

    def render(self):
        if not self.console:
            return "Scanner dashboard unavailable"
        panel_table = Table.grid(expand=True)
        panel_table.add_row(self._render_stats_panel())
        panel_table.add_row(self._render_deals_table())
        panel_table.add_row(self._render_last_table())
        return panel_table

    def refresh(self):
        if self.live:
            self.live.update(self.render())


class AlertsManager:
    def __init__(self, webhook_url="", alert_discord=False):
        self.webhook_url = webhook_url.strip() if webhook_url else ""
        self.alert_discord = bool(alert_discord)
        self.can_notify = bool(self.alert_discord and self.webhook_url and DiscordWebhook and DiscordEmbed)
        self.dashboard = Dashboard() if Console else None

    def start(self):
        if self.dashboard:
            self.dashboard.start()

    def stop(self):
        if self.dashboard:
            self.dashboard.stop()

    def update_stats(self, scanned, deals):
        if self.dashboard:
            self.dashboard.update_stats(scanned, deals)

    def log_listing(self, listing, result, parsed):
        summary = result
        card_name = parsed.get("cardName") if parsed else None
        entry = {"card_name": card_name or listing.get("title") if listing else "Search", "result": summary}
        if self.dashboard:
            self.dashboard.log_listing(entry)
        else:
            print(f"[Listing] {entry['card_name']}: {entry['result']}")

    def add_deal(self, title, url, ebay_price, tcg_price, ebay_sold_avg, foreign_price, score, level, parsed):
        summary_parts = [f"eBay {ebay_price:.2f}"]
        if tcg_price is not None:
            summary_parts.append(f"TCG {tcg_price:.2f}")
        if ebay_sold_avg is not None:
            summary_parts.append(f"Sold {ebay_sold_avg:.2f}")
        if foreign_price is not None:
            summary_parts.append(f"Foreign {foreign_price:.2f}")
        summary = " | ".join(summary_parts)

        deal = {
            "score": score,
            "level": level,
            "title": title,
            "card_name": parsed.get("cardName") if parsed else title,
            "summary": summary,
        }

        if self.dashboard:
            self.dashboard.add_deal(deal)
        else:
            print(f"{level} {title}")
            print(f"  {summary} | Score: {score:.1f}")
            print(f"  URL: {url}")

    def send_discord_notification(self, title, url, ebay_price, tcg_price, ebay_sold_avg, foreign_price, score, level, image_url=None, parsed=None):
        if not self.can_notify:
            return False

        embed = DiscordEmbed(
            title=level,
            description=f"**{title}**\n\n**eBay:** ${ebay_price:.2f}\n"
                        f"**TCGPlayer:** {format(tcg_price, '.2f') if tcg_price is not None else 'N/A'}\n"
                        f"**Sold Avg:** {format(ebay_sold_avg, '.2f') if ebay_sold_avg is not None else 'N/A'}\n"
                        f"**Foreign Comp:** {format(foreign_price, '.2f') if foreign_price is not None else 'N/A'}\n"
                        f"**Score:** {score:.1f}",
            color=16753920,
        )
        embed.add_embed_field(name="Listing", value=f"[Open on eBay]({url})", inline=False)
        if parsed:
            field_value = " | ".join(filter(None, [parsed.get("language"), parsed.get("setName"), parsed.get("cardNumber")]))
            if field_value:
                embed.add_embed_field(name="Parsed", value=field_value, inline=False)
        if image_url:
            embed.set_thumbnail(url=image_url)

        webhook = DiscordWebhook(url=self.webhook_url)
        webhook.add_embed(embed)
        try:
            response = webhook.execute()
            return response.status_code in (200, 204)
        except Exception:
            return False
