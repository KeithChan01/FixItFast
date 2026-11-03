"""Postal code -> planning region lookup table (H01).

A Singapore postal code's first two digits are its postal sector. Sectors are
grouped into the five planning regions used by the pricing model. The grouping
follows postal districts and is approximate where a district spans two regions.
"""
from catalogue.models import PlanningRegion

_SECTORS = {
    PlanningRegion.CENTRAL: [
        '01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '14', '15', '16',
        '17', '18', '19', '20', '21', '22', '23', '24', '25', '26', '27', '28', '29',
        '30', '31', '32', '33', '34', '35', '36', '37', '57',
    ],
    PlanningRegion.EAST: [
        '38', '39', '40', '41', '42', '43', '44', '45', '46', '47', '48', '49', '50',
        '51', '52', '81',
    ],
    PlanningRegion.NORTH_EAST: ['53', '54', '55', '56', '79', '80', '82'],
    PlanningRegion.NORTH: ['72', '73', '75', '76', '77', '78'],
    PlanningRegion.WEST: [
        '11', '12', '13', '58', '59', '60', '61', '62', '63', '64', '65', '66', '67',
        '68', '69', '70', '71',
    ],
}

SECTOR_TO_REGION = {sector: region for region, sectors in _SECTORS.items() for sector in sectors}


def region_for_postal_code(postal_code):
    """Return the PlanningRegion value for a 6-digit postal code, or None if unknown."""
    if not (len(postal_code) == 6 and postal_code.isdigit()):
        return None
    return SECTOR_TO_REGION.get(postal_code[:2])
