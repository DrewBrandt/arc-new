// arc_router.c
// Implementation of the ARC static frame router.

#include "arc_router.h"

#include <string.h>

void arc_router_init(arc_router_t* r,
                     uint8_t my_addr,
                     arc_router_local_fn local_fn,
                     void* local_user)
{
    memset(r, 0, sizeof(*r));
    r->my_addr          = my_addr;
    r->default_link_idx = -1;
    r->local_fn         = local_fn;
    r->local_user       = local_user;
}

int arc_router_add_link(arc_router_t* r,
                        arc_router_link_send_fn send_fn,
                        void* user)
{
    for (int i = 0; i < ARC_ROUTER_MAX_LINKS; i++) {
        if (!r->links[i].in_use) {
            r->links[i].in_use  = true;
            r->links[i].send_fn = send_fn;
            r->links[i].user    = user;
            return i;
        }
    }
    return ARC_ERR_BUFFER;
}

arc_result_t arc_router_add_route(arc_router_t* r,
                                  uint8_t dst,
                                  int link_idx)
{
    if (link_idx < 0 || link_idx >= ARC_ROUTER_MAX_LINKS) return ARC_ERR_BAD_ARG;
    if (!r->links[link_idx].in_use) return ARC_ERR_BAD_ARG;

    // Replace existing route for this dst, if any.
    for (size_t i = 0; i < ARC_ROUTER_MAX_ROUTES; i++) {
        arc_router_route_t* e = &r->routes[i];
        if (e->in_use && e->dst == dst) {
            e->link_idx = (uint8_t)link_idx;
            return ARC_OK;
        }
    }
    // Otherwise allocate.
    for (size_t i = 0; i < ARC_ROUTER_MAX_ROUTES; i++) {
        arc_router_route_t* e = &r->routes[i];
        if (!e->in_use) {
            e->in_use   = true;
            e->dst      = dst;
            e->link_idx = (uint8_t)link_idx;
            return ARC_OK;
        }
    }
    return ARC_ERR_BUFFER;
}

arc_result_t arc_router_set_default(arc_router_t* r, int link_idx)
{
    if (link_idx < 0) {
        r->default_link_idx = -1;
        return ARC_OK;
    }
    if (link_idx >= ARC_ROUTER_MAX_LINKS) return ARC_ERR_BAD_ARG;
    if (!r->links[link_idx].in_use) return ARC_ERR_BAD_ARG;
    r->default_link_idx = (int8_t)link_idx;
    return ARC_OK;
}

int arc_router_route(arc_router_t* r, const arc_frame_t* frame)
{
    if (frame->dst == r->my_addr) {
        if (r->local_fn != NULL) r->local_fn(r->local_user, frame);
        return ARC_ROUTE_LOCAL;
    }

    int link_idx = -1;
    for (size_t i = 0; i < ARC_ROUTER_MAX_ROUTES; i++) {
        const arc_router_route_t* e = &r->routes[i];
        if (e->in_use && e->dst == frame->dst) {
            link_idx = e->link_idx;
            break;
        }
    }
    if (link_idx < 0) link_idx = r->default_link_idx;
    if (link_idx < 0) return ARC_ERR_BAD_ARG;

    arc_router_link_t* link = &r->links[link_idx];
    if (!link->in_use || link->send_fn == NULL) return ARC_ERR_BAD_ARG;

    link->send_fn(link->user, frame);
    return ARC_ROUTE_FORWARDED;
}
