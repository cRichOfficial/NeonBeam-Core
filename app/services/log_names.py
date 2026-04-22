# hardware_comm/app/services/log_names.py
# Single source of truth for logger names used across the services layer.
# Keeping constants here avoids circular imports between services/ and app/.

SERIAL_TX_LOGGER = "hardware_comm.serial.tx"   # data sent   → MCU
SERIAL_RX_LOGGER = "hardware_comm.serial.rx"   # data received ← MCU
