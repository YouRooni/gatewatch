import ipaddress


class Geo:
    def __init__(self, config):
        self.city = self.asn = None
        if config.get("city_database") or config.get("asn_database"):
            import geoip2.database
            if config.get("city_database"):
                self.city = geoip2.database.Reader(config["city_database"])
            if config.get("asn_database"):
                self.asn = geoip2.database.Reader(config["asn_database"])
        self.cache = {}

    def lookup(self, address):
        if not address or not ipaddress.ip_address(address).is_global:
            return ""
        if address in self.cache:
            return self.cache[address][0]
        parts = []
        country = ''
        if self.city:
            from geoip2.errors import AddressNotFoundError
            try:
                city = self.city.city(address)
                country = city.country.iso_code or ''
                parts += [part for part in (city.country.name, city.city.name) if part]
            except AddressNotFoundError:
                pass
        if self.asn:
            from geoip2.errors import AddressNotFoundError
            try:
                asn = self.asn.asn(address)
                parts.append(f"AS{asn.autonomous_system_number} {asn.autonomous_system_organization or ''}".strip())
            except AddressNotFoundError:
                pass
        result = " · ".join(parts)[:240]
        if len(self.cache) >= 2048:
            self.cache.pop(next(iter(self.cache)))
        self.cache[address] = (result,country)
        return result

    def country_code(self, address):
        self.lookup(address)
        return self.cache.get(address, ('',''))[1]

    def close(self):
        for reader in (self.city, self.asn):
            if reader:
                reader.close()
