"""NetBox custom scripts used by netbox-mcp's lab seed and integration tests.

Uploaded to NetBox by scripts/seed_lab.py (Customization > Scripts > "mcp_lab").
"""

from dcim.models import Device, Site
from extras.scripts import ObjectVar, Script, StringVar


class SiteDeviceCount(Script):
    class Meta:
        name = "MCP site device count"
        description = "Count active devices at a site (read-only)"

    site = ObjectVar(model=Site)

    def run(self, data, commit):
        devices = Device.objects.filter(site=data["site"], status="active").order_by("name")
        for device in devices:
            self.log_info(f"{device.name}: {device.role}")
        return f"{devices.count()} active devices at {data['site']}"


class SetDescription(Script):
    class Meta:
        name = "MCP set device description"
        description = "Set a device's description (run with commit=false for a dry run)"
        commit_default = False

    device = ObjectVar(model=Device)
    # Not "description": that would shadow the script's own description and break the API listing.
    new_description = StringVar(max_length=200)

    def run(self, data, commit):
        device = data["device"]
        old = device.description
        device.snapshot()
        device.description = data["new_description"]
        device.full_clean()
        device.save()
        self.log_success(f"{device.name}: description {old!r} -> {device.description!r}")
        return "changes saved" if commit else "dry run: changes rolled back"
