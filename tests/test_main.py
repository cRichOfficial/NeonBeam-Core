import pytest
from httpx import AsyncClient, ASGITransport
import io
from app.main import app

@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/health")
    # Will be 'disconnected' in tests unless serial mocked
    assert response.status_code == 200
    assert "service" in response.json()

@pytest.mark.asyncio
async def test_get_settings():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/settings")
    assert response.status_code == 200
    assert "data" in response.json()

@pytest.mark.asyncio
async def test_upload_gcode_disconnected():
    # When unit testing, serial port is generally disconnected naturally
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        dummy_file = io.BytesIO(b"G0 X0 Y0\nG1 X10 Y10 Z0 F100\n")
        response = await ac.post("/api/gcode/upload", files={"file": ("test.gcode", dummy_file, "text/plain")})
        
    assert response.status_code == 200
    # Expected disconnected message from the mocked handler
    assert response.json() == {"status": "error", "message": "Machine not connected"}
