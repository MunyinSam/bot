import discord
from discord import ui

class ContractModal(ui.Modal, title="Contract"):
    name = ui.TextInput(
        label="Contract Title",
        placeholder="No Doomscrolling for 1 week",
        required=True,
    )
    terms = ui.TextInput(
        label="Contract Terms",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Contract from **{self.name.value}**:\n{self.terms.value}",
            ephemeral=True,
        )