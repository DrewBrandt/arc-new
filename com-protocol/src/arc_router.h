// arc_router.h
// Static destination-based router for ARC frames.
//
// Mirrors control-plane/arc/router.py. Each ARC node owns one router
// instance. Frames whose dst matches my_addr are dispatched to a local
// handler; everything else is forwarded onto a registered link.

#ifndef ARC_ROUTER_H
#define ARC_ROUTER_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifndef ARC_ROUTER_MAX_LINKS
#define ARC_ROUTER_MAX_LINKS 6
#endif

#ifndef ARC_ROUTER_MAX_ROUTES
#define ARC_ROUTER_MAX_ROUTES 16
#endif

// ----------------------------------------------------------------------
// Callbacks supplied by the embedder.
//
// link_send_fn: forward a frame onto a specific link (TCP, UART, ...).
//   Each link registered with the router carries its own send_fn and
//   user-pointer.
// local_fn: dispatch a frame addressed to my_addr.
// ----------------------------------------------------------------------
typedef void (*arc_router_link_send_fn)(void* user, const arc_frame_t* frame);
typedef void (*arc_router_local_fn)(void* user, const arc_frame_t* frame);

typedef struct {
    bool                   in_use;
    arc_router_link_send_fn send_fn;
    void*                  user;
} arc_router_link_t;

typedef struct {
    bool    in_use;
    uint8_t dst;
    uint8_t link_idx;
} arc_router_route_t;

typedef struct {
    uint8_t my_addr;
    int8_t  default_link_idx;  // -1 if none

    arc_router_local_fn local_fn;
    void*               local_user;

    arc_router_link_t  links[ARC_ROUTER_MAX_LINKS];
    arc_router_route_t routes[ARC_ROUTER_MAX_ROUTES];
} arc_router_t;

typedef enum {
    ARC_ROUTE_LOCAL     = 0,
    ARC_ROUTE_FORWARDED = 1,
} arc_route_action_t;

// ----------------------------------------------------------------------
// Initialise a router. local_fn may be NULL if this node never
// terminates frames itself (rare; usually wired to arc_reliable_receive).
// ----------------------------------------------------------------------
void arc_router_init(arc_router_t* r,
                     uint8_t my_addr,
                     arc_router_local_fn local_fn,
                     void* local_user);

// ----------------------------------------------------------------------
// Register a link. Returns the assigned link index (>= 0) on success,
// or ARC_ERR_BUFFER if the link table is full. The returned index is
// what arc_router_add_route / arc_router_set_default expect.
// ----------------------------------------------------------------------
int arc_router_add_link(arc_router_t* r,
                        arc_router_link_send_fn send_fn,
                        void* user);

// ----------------------------------------------------------------------
// Add a static route: frames addressed to dst go via link_idx.
// Replaces any existing route for the same dst.
// Returns ARC_OK, ARC_ERR_BAD_ARG (link_idx out of range), or
// ARC_ERR_BUFFER (route table full).
// ----------------------------------------------------------------------
arc_result_t arc_router_add_route(arc_router_t* r,
                                  uint8_t dst,
                                  int link_idx);

// ----------------------------------------------------------------------
// Set a fallback link for unknown destinations. Pass -1 to clear.
// ----------------------------------------------------------------------
arc_result_t arc_router_set_default(arc_router_t* r, int link_idx);

// ----------------------------------------------------------------------
// Route a frame. Returns ARC_ROUTE_LOCAL or ARC_ROUTE_FORWARDED on
// success, or a negative arc_result_t when no route is configured for
// the destination (ARC_ERR_BAD_ARG).
// ----------------------------------------------------------------------
int arc_router_route(arc_router_t* r, const arc_frame_t* frame);

#ifdef __cplusplus
}
#endif

#endif  // ARC_ROUTER_H
