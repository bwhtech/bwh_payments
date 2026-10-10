from pydantic import BaseModel, ConfigDict


class GatewayModel(BaseModel):
	"""Gateways sometimes send ids or amounts as bare numbers; read them as strings."""

	model_config = ConfigDict(coerce_numbers_to_str=True)
