#include <cstdint>
#include <mmio/mmio.h>

mmio_ptr<volatile uint32_t> P2_OUT {0x50050400};
mmio_ptr<volatile uint32_t> P2_PIN_CNF {0x50050480};

mmio_ptr<volatile uint32_t> TAMPC_PROTECT_DOMAIN0_x {0x500dc500};
mmio_ptr<volatile uint32_t> TAMPC_PROTECT_AP0_x {0x500dc700};

int main() {
    TAMPC_PROTECT_DOMAIN0_x[0] = 0x50fa00f0; // DBGEN
    TAMPC_PROTECT_DOMAIN0_x[0] = 0x50fa0001; // DBGEN
    TAMPC_PROTECT_DOMAIN0_x[2] = 0x50fa00f0; // NIDEN
    TAMPC_PROTECT_DOMAIN0_x[2] = 0x50fa0001; // NIDEN
    TAMPC_PROTECT_DOMAIN0_x[4] = 0x50fa00f0; // SPIDEN
    TAMPC_PROTECT_DOMAIN0_x[4] = 0x50fa0001; // SPIDEN
    TAMPC_PROTECT_DOMAIN0_x[6] = 0x50fa00f0; // SPNIDEN
    TAMPC_PROTECT_DOMAIN0_x[6] = 0x50fa0001; // SPNIDEN
    TAMPC_PROTECT_AP0_x[0] = 0x50fa00f0; // RISC-V DBGEN
    TAMPC_PROTECT_AP0_x[0] = 0x50fa0001; // RISC-V DBGEN

    P2_PIN_CNF[9] = 1;
    *P2_OUT ^= 1 << 9;

    while (true) {}
}
