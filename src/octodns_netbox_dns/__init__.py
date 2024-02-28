import logging
from typing import Any, Literal

import dns.rdata
import octodns.provider.base
import octodns.provider.plan
import octodns.record
import octodns.zone
import pynetbox.core.api
import pynetbox.core.response


class NetBoxDNSSource(octodns.provider.base.BaseProvider):
    """OctoDNS provider for NetboxDNS"""

    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = False
    SUPPORTS_ROOT_NS = True
    SUPPORTS_MULTIVALUE_PTR = True
    SUPPORTS: set[str] = {  # noqa
        "A",
        "AAAA",
        "AFSDB",
        "APL",
        "CAA",
        "CDNSKEY",
        "CERT",
        "CNAME",
        "DCHID",
        "DNAME",
        "DNSKEY",
        "DS",
        "HIP",
        "IPSECKEY",
        "LOC",
        "MX",
        "NAPTR",
        "NS",
        "NSEC",
        "PTR",
        "RP",
        "RRSIG",
        "SOA",
        "SPF",
        "SRV",
        "SSHFP",
        "TLSA",
        "TXT",
    }

    def __init__(
        self,
        id: int,  # noqa
        url: str,
        token: str,
        view: str | None | Literal[False] = False,
        ttl=3600,
        replace_duplicates=False,
        make_absolute=False,
        *args,
        **kwargs,
    ):
        """initialize the NetboxDNSSource"""
        self.log = logging.getLogger(f"NetboxDNSSource[{id}]")
        self.log.debug(f"__init__: {id=}, {url=}, {view=}, {replace_duplicates=}, {make_absolute=}")
        super().__init__(id, *args, **kwargs)

        self.api = pynetbox.core.api.Api(url, token)
        self.nb_view = self._get_nb_view(view)
        self.ttl = ttl
        self.replace_duplicates = replace_duplicates
        self.make_absolute = make_absolute

    def _make_absolute(self, value: str) -> str:
        """return dns name with trailing dot to make it absolute

        @param value: dns record value

        @return: absolute dns record value
        """
        if not self.make_absolute or value.endswith("."):
            return value

        absolute_value = value + "."
        self.log.debug(f"relative={value}, absolute={absolute_value}")

        return absolute_value

    def _get_nb_view(self, view: str | None | Literal[False]) -> dict[str, int | str]:
        """get the correct netbox view when requested

        @param view: `False` for no view, `None` for zones without a view, else the view name

        @return: the netbox view id in the netbox query format
        """
        if view is False:
            return {}
        if view is None:
            return {"view": "null"}

        nb_view: pynetbox.core.response.Record = self.api.plugins.netbox_dns.views.get(name=view)
        if nb_view is None:
            msg = f"dns view={view}, has not been found"
            self.log.error(msg)
            raise ValueError(msg)

        self.log.debug(f"found view={nb_view.name}, id={nb_view.id}")

        return {"view_id": nb_view.id}

    def _get_nb_zone(self, name: str, view: dict[str, str | int]) -> pynetbox.core.response.Record:
        """given a zone name and a view name, look it up in NetBox.

        @param name: name of the dns zone
        @param view: the netbox view id in the api query format

        @raise pynetbox.RequestError: if declared view is not existent

        @return: the netbox dns zone object
        """
        query_params = {"name": name[:-1], **view}
        nb_zone = self.api.plugins.netbox_dns.zones.get(**query_params)

        self.log.debug(f"found zone={nb_zone.name}, id={nb_zone.id}")

        return nb_zone

    def _format_rdata(
        self, nb_record: pynetbox.core.response.Record, raw_value: str
    ) -> str | dict[str, Any]:
        """format netbox record values to correct octodns record values

        @param nb_record: netbox record
        @param raw_value: raw record value

        @return: formatted rrdata value
        """
        rdata = dns.rdata.from_text("IN", nb_record.type, raw_value)
        match rdata.rdtype.name:
            case "A" | "AAAA":
                value = rdata.address

            case "CNAME":
                value = self._make_absolute(rdata.target.to_text())

            case "DNAME" | "NS" | "PTR":
                value = rdata.target.to_text()

            case "CAA":
                value = {
                    "flags": rdata.flags,
                    "tag": rdata.tag,
                    "value": rdata.value,
                }

            case "LOC":
                value = {
                    "lat_direction": "N" if rdata.latitude[4] >= 0 else "S",
                    "lat_degrees": rdata.latitude[0],
                    "lat_minutes": rdata.latitude[1],
                    "lat_seconds": rdata.latitude[2] + rdata.latitude[3] / 1000,
                    "long_direction": "W" if rdata.latitude[4] >= 0 else "E",
                    "long_degrees": rdata.longitude[0],
                    "long_minutes": rdata.longitude[1],
                    "long_seconds": rdata.longitude[2] + rdata.longitude[3] / 1000,
                    "altitude": rdata.altitude / 100,
                    "size": rdata.size / 100,
                    "precision_horz": rdata.horizontal_precision / 100,
                    "precision_vert": rdata.veritical_precision / 100,
                }

            case "MX":
                value = {
                    "preference": rdata.preference,
                    "exchange": self._make_absolute(rdata.exchange.to_text()),
                }

            case "NAPTR":
                value = {
                    "order": rdata.order,
                    "preference": rdata.preference,
                    "flags": rdata.flags,
                    "service": rdata.service,
                    "regexp": rdata.regexp,
                    "replacement": rdata.replacement.to_text(),
                }

            case "SSHFP":
                value = {
                    "algorithm": rdata.algorithm,
                    "fingerprint_type": rdata.fp_type,
                    "fingerprint": rdata.fingerprint,
                }

            case "SPF" | "TXT":
                value = raw_value.replace(";", "\\;")

            case "SRV":
                value = {
                    "priority": rdata.priority,
                    "weight": rdata.weight,
                    "port": rdata.port,
                    "target": self._make_absolute(rdata.target.to_text()),
                }

            case "SOA":
                self.log.debug("SOA record type not implemented")
                raise NotImplementedError

            case _:
                self.log.error("invalid record type")
                raise ValueError

        self.log.debug(f"formatted record value={value}")

        return value  # type:ignore

    def _format_nb_records(self, zone: octodns.zone.Zone) -> list[dict[str, Any]]:
        """format netbox dns records to the octodns format

        @param zone: octodns zone

        @return: a list of octodns compatible record dicts
        """
        records: dict[tuple[str, str], dict[str, Any]] = {}

        nb_zone = self._get_nb_zone(zone.name, view=self.nb_view)
        if not nb_zone:
            self.log.error(f"zone={zone.name}, not found in view={self.nb_view}")
            raise LookupError

        nb_records: pynetbox.core.response.RecordSet = self.api.plugins.netbox_dns.records.filter(
            zone_id=nb_zone.id, status="active"
        )
        for nb_record in nb_records:
            rcd_name: str = nb_record.name if nb_record.name != "@" else ""
            raw_value: str = nb_record.value if nb_record.value != "@" else nb_record.zone.name
            rcd_type: str = nb_record.type
            rcd_ttl: int = nb_record.ttl or nb_zone.default_ttl
            if nb_record.type == "NS":
                rcd_ttl = nb_zone.soa_refresh

            rcd_data = {
                "name": rcd_name,
                "type": rcd_type,
                "ttl": rcd_ttl,
                "values": [],
            }

            self.log.debug(f"record data={rcd_data}")

            try:
                rcd_value = self._format_rdata(nb_record, raw_value)
            except NotImplementedError:
                continue
            except Exception as exc:
                raise exc

            if (rcd_name, rcd_type) not in records:
                records[(rcd_name, rcd_type)] = rcd_data

            records[(rcd_name, rcd_type)]["values"].append(rcd_value)

        return list(records.values())

    def populate(
        self, zone: octodns.zone.Zone, target: bool = False, lenient: bool = False
    ) -> bool:
        """get all the records of a zone from NetBox and add them to the OctoDNS zone

        @param zone: octodns zone
        @param target: when `True`, load the current state of the provider.
        @param lenient: when `True`, skip record validation and do a "best effort" load of data.

        @return: true if the zone exists, else false.
        """
        self.log.info(f"populate -> '{zone.name}', target={target}, lenient={lenient}")

        try:
            records = self._format_nb_records(zone)
        except LookupError:
            return False

        for data in records:
            if len(data["values"]) == 1:
                data["value"] = data.pop("values")[0]
            if target and data["type"] in ["NS", "SOA", "PTR"]:
                self.log.debug(f"{data['type']} type not supported in target mode")
                continue
            record = octodns.record.Record.new(
                zone=zone,
                name=data["name"],
                data=data,
                source=self,
                lenient=lenient,
            )
            zone.add_record(record, lenient=lenient, replace=self.replace_duplicates)

        self.log.info(f"populate -> found {len(zone.records)} records for zone '{zone.name}'")

        return True

    @staticmethod
    def _format_changeset(change: Any) -> set[str]:
        """format the changeset

        @param change: the raw changes

        @return: the formatted changeset
        """
        match change:
            case octodns.record.ValueMixin():
                changeset = {repr(change.value)[1:-1]}
            case octodns.record.ValuesMixin():
                changeset = {repr(v)[1:-1] for v in change.values}
            case _:
                raise ValueError

        return changeset

    @staticmethod
    def _include_change(change: octodns.record.change.Change) -> bool:
        """filter out record types which the provider can't create in netbox
        @param change: the planned change

        @return: false if the change should be discarded, true if it should be kept.
        """
        if change.record._type in ["SOA", "PTR", "NS"]:
            return False

        return True

    def _apply(self, plan: octodns.provider.plan.Plan) -> None:
        """apply the changes to the NetBox DNS zone."""
        self.log.debug(f"_apply: zone={plan.desired.name}, changes={len(plan.changes)}")

        nb_zone = self._get_nb_zone(plan.desired.name, view=self.nb_view)

        for change in plan.changes:
            match change:
                case octodns.record.Create():
                    rcd_name = "@" if change.new.name == "" else change.new.name

                    new_changeset = self._format_changeset(change.new)
                    for record in new_changeset:
                        nb_record: pynetbox.core.response.Record = (
                            self.api.plugins.netbox_dns.records.create(
                                zone=nb_zone.id,
                                name=rcd_name,
                                type=change.new._type,
                                ttl=change.new.ttl,
                                value=record.replace("\\\\", "\\").replace("\\;", ";"),
                                disable_ptr=True,
                            )
                        )
                        self.log.debug(f"{nb_record!r}")

                case octodns.record.Delete():
                    nb_records: pynetbox.core.response.RecordSet = (
                        self.api.plugins.netbox_dns.records.filter(
                            zone_id=nb_zone.id,
                            name=change.existing.name,
                            type=change.existing._type,
                        )
                    )

                    existing_changeset = self._format_changeset(change.existing)
                    for nb_record in nb_records:
                        for record in existing_changeset:
                            if nb_record.value == record:
                                self.log.debug(
                                    f"{nb_record.id} {nb_record.name} {nb_record.type} {nb_record.value} {record}"
                                )
                                self.log.debug(f"{nb_record.url} {nb_record.endpoint.url}")
                                nb_record.delete()

                case octodns.record.Update():
                    rcd_name = "@" if change.existing.name == "" else change.existing.name

                    nb_records: pynetbox.core.response.RecordSet = (
                        self.api.plugins.netbox_dns.records.filter(
                            zone_id=nb_zone.id,
                            name=rcd_name,
                            type=change.existing._type,
                        )
                    )

                    existing_changeset = self._format_changeset(change.existing)
                    new_changeset = self._format_changeset(change.new)

                    to_delete = existing_changeset.difference(new_changeset)
                    to_update = existing_changeset.intersection(new_changeset)
                    to_create = new_changeset.difference(existing_changeset)

                    for nb_record in nb_records:
                        if nb_record.value in to_delete:
                            nb_record.delete()
                        if nb_record.value in to_update:
                            nb_record.ttl = change.new.ttl
                            nb_record.save()

                    for record in to_create:
                        nb_record: pynetbox.core.response.Record = (
                            self.api.plugins.netbox_dns.records.create(
                                zone=nb_zone.id,
                                name=rcd_name,
                                type=change.new._type,
                                ttl=change.new.ttl,
                                value=record.replace("\\\\", "\\").replace("\\;", ";"),
                                disable_ptr=True,
                            )
                        )
                        self.log.debug(f"{nb_record!r}")
