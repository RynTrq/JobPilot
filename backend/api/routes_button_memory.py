"""Button name memory API endpoints for registering and managing alternative button names."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.storage.button_memory import ButtonNameMemory

router = APIRouter(prefix="/api/v1/button-memory", tags=["button-memory"])

_button_memory: ButtonNameMemory | None = None


def get_button_memory() -> ButtonNameMemory:
    """Dependency: Get the button name memory instance."""
    global _button_memory
    if _button_memory is None:
        _button_memory = ButtonNameMemory()
    return _button_memory


@router.post("/register")
async def register_button_name(
    button_type: str,
    name: str,
    domain: str | None = None,
    memory: ButtonNameMemory = Depends(get_button_memory),
) -> dict[str, Any]:
    """
    Register an alternative button name for future form navigation.
    
    - **button_type**: "submit" or "next"
    - **name**: The alternative button text (e.g., "Apply now")
    - **domain**: Optional domain where this was encountered (e.g., "tensorgo.com")
    """
    try:
        memory.register_button_name(button_type, name, domain)
        return {
            "status": "registered",
            "button_type": button_type,
            "name": name,
            "domain": domain,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/list")
async def list_button_names(
    button_type: str | None = None,
    memory: ButtonNameMemory = Depends(get_button_memory),
) -> dict[str, Any]:
    """
    List all registered button name mappings.
    
    - **button_type**: Optional filter (e.g., "submit" to see only submit button alternatives)
    """
    all_mappings = memory.get_all_mappings()
    
    if button_type:
        filtered = [m for m in all_mappings if m["button_type"] == button_type]
    else:
        filtered = all_mappings
    
    return {
        "status": "success",
        "total": len(all_mappings),
        "filtered": len(filtered),
        "mappings": filtered,
    }


@router.post("/clear")
async def clear_button_memory(
    memory: ButtonNameMemory = Depends(get_button_memory),
) -> dict[str, Any]:
    """Clear all registered button names."""
    memory.clear_memory()
    return {"status": "cleared"}
