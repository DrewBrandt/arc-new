// hub_map.h
// Address/link names for the ARC Teensy hub. Runtime routing learns live UART
// locations from source traffic; there is no static address-to-port map.

#ifndef HUB_MAP_H
#define HUB_MAP_H

#include <stddef.h>
#include <stdint.h>

#include <Arduino.h>

#include "arc_protocol.h"

enum HubRouteLink : int8_t {
    HUB_ROUTE_LINK_SPOKE1 = 0,
    HUB_ROUTE_LINK_SPOKE2,
    HUB_ROUTE_LINK_SPOKE3,
    HUB_ROUTE_LINK_SPOKE4,
    HUB_ROUTE_LINK_SPOKE5,
    HUB_ROUTE_LINK_SPOKE6,
    HUB_ROUTE_LINK_SPOKE7,
    HUB_ROUTE_LINK_COUNT,

    HUB_ROUTE_LINK_UNKNOWN = -1,
    HUB_ROUTE_LINK_LOCAL = -2,
    HUB_ROUTE_LINK_BROADCAST = -3,
};

const char* hub_route_link_name(HubRouteLink link);
const char* hub_addr_name(uint8_t addr);

void hub_map_print(Print& out);

#endif  // HUB_MAP_H
