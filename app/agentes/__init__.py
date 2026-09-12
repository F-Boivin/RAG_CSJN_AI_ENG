"""Los tres especialistas del equipo."""

from app.agentes.investigador import investigador_node
from app.agentes.redactor import redactor_node
from app.agentes.verificador import verificador_node

__all__ = ["investigador_node", "verificador_node", "redactor_node"]
