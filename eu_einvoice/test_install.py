from lxml import etree

from eu_einvoice.common_codes import (
	DUTY_TAX_FEE_CATEGORY_CODE_LIST,
	PAYMENT_CODE_LIST,
	UNIT_CODE_LIST,
	VAT_EXEMPTION_REASON_CODE_LIST,
)
from eu_einvoice.install import CODELIST_COLUMNS, CODELIST_DIR


def test_bundled_codelists_match_hardcoded_column_map():
	paths = sorted(CODELIST_DIR.glob("*.gc"))
	assert paths, "no code lists bundled"

	for path in paths:
		root = etree.parse(str(path)).getroot()
		columns = {c.get("Id") for c in root.findall(".//Column")}
		assert columns <= set(CODELIST_COLUMNS.values()), f"{path.name} has unknown columns {columns}"
		assert "Code" in columns, f"{path.name} has no Code column"
		assert root.findtext(".//CanonicalVersionUri"), f"{path.name} has no CanonicalVersionUri"


def test_configured_code_lists_are_bundled():
	bundled_uris = {
		etree.parse(str(path)).findtext(".//CanonicalVersionUri") for path in CODELIST_DIR.glob("*.gc")
	}

	assert {
		UNIT_CODE_LIST,
		PAYMENT_CODE_LIST,
		DUTY_TAX_FEE_CATEGORY_CODE_LIST,
		VAT_EXEMPTION_REASON_CODE_LIST,
	} <= bundled_uris
