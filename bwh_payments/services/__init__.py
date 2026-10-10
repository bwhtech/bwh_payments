from pydantic import BaseModel, ConfigDict


class GatewayModel(BaseModel):
	"""Base for models that mirror a gateway's JSON. Unknown fields are ignored (pydantic's default), and a
	number arrives as a string where the gateway sometimes sends ids or amounts as bare numbers."""

	model_config = ConfigDict(coerce_numbers_to_str=True)
