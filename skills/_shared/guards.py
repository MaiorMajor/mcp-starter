"""Guard rails partilhados entre skills.

Circuit breaker via ficheiro `meta/AGENT_PAUSE` no vault: se existir,
qualquer skill que invoque `check_circuit_breaker()` aborta imediatamente
com exit 0 (bloqueio é intencional, não é erro).
"""
import os
import sys

VAULT_ROOT = os.environ.get("VAULT_PATH") or os.environ.get("VAULT_ROOT")
if not VAULT_ROOT:
    VAULT_ROOT = ""

AGENT_PAUSE_REL = os.path.join("meta", "AGENT_PAUSE")


def is_paused(vault_root: str = VAULT_ROOT) -> tuple[bool, str]:
    """Devolve (paused, reason). Não levanta excepções — lê best-effort."""
    flag = os.path.join(vault_root, AGENT_PAUSE_REL)
    if not os.path.exists(flag):
        return False, ""
    try:
        with open(flag, encoding="utf-8") as f:
            reason = f.read().strip()
    except Exception:
        reason = ""
    return True, reason or "sem motivo especificado"


def check_circuit_breaker(vault_root: str = VAULT_ROOT) -> None:
    """Aborta a skill se existir o ficheiro AGENT_PAUSE no vault."""
    paused, reason = is_paused(vault_root)
    if paused:
        print(f"[CIRCUIT BREAKER] Execução bloqueada: {reason}", file=sys.stderr)
        sys.exit(0)
