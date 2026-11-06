"""AssetValuationDataRoom: registry-fed rNPV valuation and diligence data room builder.

Reads an asset lineage record (AssetProgramRegistry contract), derives rNPV
inputs with a recorded source for every field, values the asset with
thesis-generator's rNPV engine, exports to thesis-generator, DealStructureSimulator
and PharmaEventBus, and assembles a diligence data room.
"""

__version__ = "0.1.0"
