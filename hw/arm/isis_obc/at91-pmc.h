/*
 * AT91 Power Management Controller.
 *
 * Controlls AT91 system master clock.
 *
 * Notes: Register callback via at91_pmc_set_mclk_change_callback to get
 * notified when sytem clock changes. Only one callback allowed at a time.
 * This should be done by the board implementation.
 *
 * See at91-pmc.c for implementation status.
 *
 * Copyright (c) 2019-2020 KSat e.V. Stuttgart
 *
 * This work is licensed under the terms of the GNU GPL, version 2 or, at your
 * option, any later version. See the COPYING file in the top-level directory.
 */

#ifndef HW_ARM_ISIS_OBC_PMC_H
#define HW_ARM_ISIS_OBC_PMC_H

#include "qemu/osdep.h"
#include "hw/sysbus.h"


#define AT91_PMC_SLCK          32768    // slow clock oscillator frequency
#define AT91_PMC_MCK        18432000    // main oscillator frequency

#define TYPE_AT91_PMC "at91-pmc"
#define AT91_PMC(obj) OBJECT_CHECK(At91Pmc, (obj), TYPE_AT91_PMC)


typedef void(at91_mclk_cb)(void *opaque, unsigned value);

typedef struct {
    uint32_t reg_ckgr_mor;
    uint32_t reg_ckgr_plla;
    uint32_t reg_ckgr_pllb;
    uint32_t reg_pmc_mckr;
} At91PmcInitState;

typedef struct {
    SysBusDevice parent_obj;

    MemoryRegion mmio;
    qemu_irq irq;

    const At91PmcInitState *init_state;

    // registers
    uint32_t reg_pmc_scsr;
    uint32_t reg_pmc_pcsr;
    uint32_t reg_ckgr_mor;
    uint32_t reg_ckgr_mcfr;
    uint32_t reg_ckgr_plla;
    uint32_t reg_ckgr_pllb;
    uint32_t reg_pmc_mckr;
    uint32_t reg_pmc_pck0;
    uint32_t reg_pmc_pck1;
    uint32_t reg_pmc_sr;
    uint32_t reg_pmc_imr;
    uint32_t reg_pmc_pllicpr;

    unsigned master_clock_freq;

    // observer for master-clock change
    at91_mclk_cb *mclk_cb;
    void *mclk_opaque;
} At91Pmc;


/*
 * Set the callback function to be called when the AT91 master clock changes.
 * Only one callback can be set at a time.
 */
inline static void at91_pmc_set_mclk_change_callback(At91Pmc *s, void *opaque, at91_mclk_cb *cb)
{
    s->mclk_cb = cb;
    s->mclk_opaque = opaque;
}

inline static void at91_pmc_set_init_state(At91Pmc *s, const At91PmcInitState *init)
{
    s->init_state = init;
}

#endif /* HW_ARM_ISIS_OBC_PWC_H */
