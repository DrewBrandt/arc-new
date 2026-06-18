// hub_map.cpp -- see hub_map.h.

#include "hub_map.h"

const char* hub_route_link_name(HubRouteLink link) {
    switch (link) {
        case HUB_ROUTE_LINK_SPOKE1:     return "serial2";
        case HUB_ROUTE_LINK_SPOKE2:     return "serial3";
        case HUB_ROUTE_LINK_SPOKE3:     return "serial4";
        case HUB_ROUTE_LINK_SPOKE4:     return "serial5";
        case HUB_ROUTE_LINK_SPOKE5:     return "serial6";
        case HUB_ROUTE_LINK_SPOKE6:     return "serial7";
        case HUB_ROUTE_LINK_SPOKE7:     return "serial8";
        case HUB_ROUTE_LINK_LOCAL:      return "local";
        case HUB_ROUTE_LINK_BROADCAST:  return "broadcast";
        default:                        return "?";
    }
}

const char* hub_addr_name(uint8_t addr) {
    switch (addr) {
        case ARC_ADDR_UNASSIGNED:      return "unassigned";
        case ARC_ADDR_GROUND:          return "ground";
        case ARC_ADDR_FC_N:            return "fc-n";
        case ARC_ADDR_FC_C:            return "fc-c";
        case ARC_ADDR_FC_L:            return "fc-l";
        case ARC_ADDR_TEENSY_HUB:      return "teensy-hub";
        case ARC_ADDR_CONTROLLER:      return "pi-5-nose";
        case ARC_ADDR_SENDER_DOWN:     return "sender-down";
        case ARC_ADDR_SENDER_AIRBRAKE: return "sender-airbrake";
        case ARC_ADDR_SENDER_PAYLOAD:  return "sender-payload";
        case ARC_ADDR_SENDER_GROUND:   return "sender-ground";
        case ARC_ADDR_RADIO_CMD:       return "radio-cmd";
        case ARC_ADDR_RADIO_G:         return "radio-g";
        case ARC_ADDR_RADIO_DATA:      return "radio-data";
        case ARC_ADDR_ARCH_MEGA_N:     return "arch-mega-n";
        case ARC_ADDR_ARCH_MEGA_L:     return "arch-mega-l";
        case ARC_ADDR_ARCH_MEGA_C:     return "arch-mega-c";
        case ARC_ADDR_BROADCAST:       return "broadcast";
        default:                       return "unknown";
    }
}

void hub_map_print(Print& out) {
    out.println(F("[hub] routing"));
    out.println(F("  all ARC node routes are learned from source traffic"));
    out.println(F("  unknown destination -> discovery heartbeat broadcast"));
    out.println(F("  broadcast -> all ARC UART spokes except ingress"));
    out.println(F("  Serial1 -> raw payload telemetry radio, not an ARC spoke"));
}
