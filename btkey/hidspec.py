# SPDX-License-Identifier: GPL-2.0-only
"""HID report descriptor and the SDP record that advertises it.

The descriptor declares two input reports:

  report 1: boot-compatible keyboard - 1 modifier byte, 1 reserved byte,
            6 key usage slots.  Also carries the LED output report so the
            host can drive Caps/Num Lock.
  report 2: consumer control - one 16-bit usage, for volume and media keys.

A mouse collection is deliberately absent.  Changing this descriptor later
means the iPhone must forget the device and pair again, since iOS caches
the descriptor per bonded device.
"""

from xml.sax.saxutils import escape

REPORT_ID_KEYBOARD = 1
REPORT_ID_CONSUMER = 2

REPORT_DESCRIPTOR = bytes([
    # ---- Keyboard -------------------------------------------------------
    0x05, 0x01,              # Usage Page (Generic Desktop)
    0x09, 0x06,              # Usage (Keyboard)
    0xA1, 0x01,              # Collection (Application)
    0x85, REPORT_ID_KEYBOARD,#   Report ID (1)
    0x05, 0x07,              #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,              #   Usage Minimum (Left Control)
    0x29, 0xE7,              #   Usage Maximum (Right GUI)
    0x15, 0x00,              #   Logical Minimum (0)
    0x25, 0x01,              #   Logical Maximum (1)
    0x75, 0x01,              #   Report Size (1)
    0x95, 0x08,              #   Report Count (8)
    0x81, 0x02,              #   Input (Data, Variable, Absolute) - modifiers
    0x95, 0x01,              #   Report Count (1)
    0x75, 0x08,              #   Report Size (8)
    0x81, 0x01,              #   Input (Constant) - reserved byte
    0x95, 0x06,              #   Report Count (6)
    0x75, 0x08,              #   Report Size (8)
    0x15, 0x00,              #   Logical Minimum (0)
    0x26, 0xFF, 0x00,        #   Logical Maximum (255)
    0x05, 0x07,              #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,              #   Usage Minimum (0)
    0x2A, 0xFF, 0x00,        #   Usage Maximum (255)
    0x81, 0x00,              #   Input (Data, Array) - six key slots
    0x95, 0x05,              #   Report Count (5)
    0x75, 0x01,              #   Report Size (1)
    # Logical Minimum and Maximum are global: without these two the LED
    # bits would still carry the (0, 255) left behind by the key array
    # above, on fields one bit wide.  The boot keyboard descriptor gets
    # away with omitting them only because it puts the LEDs before the key
    # array, where the modifiers' (0, 1) is still in force.
    0x15, 0x00,              #   Logical Minimum (0)
    0x25, 0x01,              #   Logical Maximum (1)
    0x05, 0x08,              #   Usage Page (LEDs)
    0x19, 0x01,              #   Usage Minimum (Num Lock)
    0x29, 0x05,              #   Usage Maximum (Kana)
    0x91, 0x02,              #   Output (Data, Variable, Absolute) - LEDs
    0x95, 0x01,              #   Report Count (1)
    0x75, 0x03,              #   Report Size (3)
    0x91, 0x01,              #   Output (Constant) - LED padding
    0xC0,                    # End Collection

    # ---- Consumer control ----------------------------------------------
    # The range goes to 0x3FF rather than to the highest usage in use.
    # It is what the phone is told the report can carry, it is read at bond
    # time and never again, and widening it later costs a re-pair; leaving
    # room is free now and expensive afterwards.
    0x05, 0x0C,              # Usage Page (Consumer)
    0x09, 0x01,              # Usage (Consumer Control)
    0xA1, 0x01,              # Collection (Application)
    0x85, REPORT_ID_CONSUMER,#   Report ID (2)
    0x15, 0x00,              #   Logical Minimum (0)
    0x26, 0xFF, 0x03,        #   Logical Maximum (0x3FF)
    0x19, 0x00,              #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,        #   Usage Maximum (0x3FF)
    0x75, 0x10,              #   Report Size (16)
    0x95, 0x01,              #   Report Count (1)
    0x81, 0x00,              #   Input (Data, Array)
    0xC0,                    # End Collection
])

# L2CAP PSMs fixed by the HID profile specification.
PSM_CONTROL = 17
PSM_INTERRUPT = 19

# Class of Device: Peripheral major class, keyboard minor class, with the
# "limited discoverable" and "object transfer" service bits real keyboards
# also set.  iOS uses this to decide the device is a keyboard.
CLASS_OF_DEVICE = 0x002540

# What goes into bluetoothd's main.conf: it only honours the major and minor
# device class bits and ORs the service class bits in itself.
MAIN_CONF_CLASS = CLASS_OF_DEVICE & 0x1FFF

# Service class bits, from the Bluetooth assigned numbers.  bluetoothd
# derives these from the registered profile UUIDs rather than from main.conf,
# and its table only maps Headset and Handsfree to Audio - A2DP Sink and
# Source map to Rendering and Capturing.  So dropping HSP/HFP to be rid of
# call audio also silently drops the Audio bit, which is the one a phone
# looks at when deciding whether this is somewhere it can send sound.
SERVICE_RENDERING = 0x040000
SERVICE_CAPTURING = 0x080000
SERVICE_AUDIO = 0x200000


def service_record(name, description, provider):
    """Build the BlueZ SDP record XML advertising us as a HID keyboard."""
    descriptor_hex = REPORT_DESCRIPTOR.hex()
    # These land inside double-quoted XML attributes, so the quote needs
    # escaping too - escape() does not do it by default.
    name, description, provider = (escape(value, {'"': "&quot;"})
                                   for value in (name, description, provider))
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001">        <!-- ServiceClassIDList -->
    <sequence><uuid value="0x1124" /></sequence>
  </attribute>
  <attribute id="0x0004">        <!-- ProtocolDescriptorList -->
    <sequence>
      <sequence>
        <uuid value="0x0100" />
        <uint16 value="0x{PSM_CONTROL:04x}" />
      </sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005">        <!-- BrowseGroupList -->
    <sequence><uuid value="0x1002" /></sequence>
  </attribute>
  <attribute id="0x0006">        <!-- LanguageBaseAttributeIDList -->
    <sequence>
      <uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" />
    </sequence>
  </attribute>
  <attribute id="0x0009">        <!-- BluetoothProfileDescriptorList -->
    <sequence>
      <sequence>
        <uuid value="0x1124" />
        <uint16 value="0x0101" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x000d">        <!-- AdditionalProtocolDescriptorLists -->
    <sequence>
      <sequence>
        <sequence>
          <uuid value="0x0100" />
          <uint16 value="0x{PSM_INTERRUPT:04x}" />
        </sequence>
        <sequence><uuid value="0x0011" /></sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100"><text value="{name}" /></attribute>
  <attribute id="0x0101"><text value="{description}" /></attribute>
  <attribute id="0x0102"><text value="{provider}" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>  <!-- HIDDeviceReleaseNumber -->
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>  <!-- HIDParserVersion -->
  <attribute id="0x0202"><uint8 value="0x40" /></attribute>     <!-- HIDDeviceSubclass: keyboard -->
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>     <!-- HIDCountryCode: not localised -->
  <attribute id="0x0204"><boolean value="true" /></attribute>   <!-- HIDVirtualCable -->
  <attribute id="0x0205"><boolean value="true" /></attribute>   <!-- HIDReconnectInitiate -->
  <attribute id="0x0206">        <!-- HIDDescriptorList -->
    <sequence>
      <sequence>
        <uint8 value="0x22" />
        <text encoding="hex" value="{descriptor_hex}" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0207">        <!-- HIDLANGIDBaseList -->
    <sequence>
      <sequence>
        <uint16 value="0x0409" /><uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0208"><boolean value="false" /></attribute>  <!-- HIDSDPDisable -->
  <attribute id="0x0209"><boolean value="false" /></attribute>  <!-- HIDBatteryPower -->
  <attribute id="0x020a"><boolean value="true" /></attribute>   <!-- HIDRemoteWake -->
  <attribute id="0x020b"><uint16 value="0x0101" /></attribute>  <!-- HIDProfileVersion -->
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>  <!-- HIDSupervisionTimeout -->
  <attribute id="0x020d"><boolean value="true" /></attribute>   <!-- HIDNormallyConnectable -->
  <attribute id="0x020e"><boolean value="true" /></attribute>   <!-- HIDBootDevice -->
</record>
"""
