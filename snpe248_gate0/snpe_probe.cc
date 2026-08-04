// Gate 0 / Gate 1 probe for moving the comma two from SNPE 1.41 to QAIRT 2.48.
//
// Touches NOTHING outside its own directory. Does not load, link against, or
// modify anything under /data/openpilot. Run it, read the output, delete the dir.
//
//   Gate 0 (no args)   -- does the 2.48 runtime initialise on Adreno 530 at all?
//   Gate 1 (dlc path)  -- can it open + graph-prepare + run an existing 2.48 .dlc?
//
// Built against the 2.48 headers, which alias everything back into zdl:: via
// ALIAS_IN_ZDL_NAMESPACE, so this is the same spelling bukapilot already uses.

#include <cstdio>
#include <chrono>
#include <memory>
#include <vector>

#include "SNPE/SNPEFactory.hpp"
#include "SNPE/SNPEBuilder.hpp"
#include "SNPE/SNPE.hpp"
#include "DlContainer/IDlContainer.hpp"
#include "DlSystem/DlEnums.hpp"
#include "DlSystem/DlError.hpp"
#include "DlSystem/RuntimeList.hpp"
#include "DlSystem/ITensorFactory.hpp"
#include "DlSystem/TensorMap.hpp"
#include "DlSystem/TensorShape.hpp"

static const char *yn(bool b) { return b ? "YES" : "no"; }

int main(int argc, char **argv) {
  using RT = zdl::DlSystem::Runtime_t;

  printf("== gate 0: runtime ==\n");
  printf("library version : %s\n",
         zdl::SNPE::SNPEFactory::getLibraryVersion().asString().c_str());

  const bool cpu = zdl::SNPE::SNPEFactory::isRuntimeAvailable(RT::CPU);
  const bool gpu = zdl::SNPE::SNPEFactory::isRuntimeAvailable(RT::GPU);
  const bool dsp = zdl::SNPE::SNPEFactory::isRuntimeAvailable(RT::DSP);
  printf("CPU available   : %s\n", yn(cpu));
  printf("GPU available   : %s   <-- the one that matters (Adreno 530)\n", yn(gpu));
  printf("DSP available   : %s   (expected no: 2.48 bottoms out at Hexagon v66/v68)\n", yn(dsp));

  if (!gpu && !cpu) {
    printf("\nRESULT: no usable runtime. The 2.48 port is dead here.\n");
    return 2;
  }
  if (argc < 2) {
    printf("\nGate 0 done. Pass a .dlc path to also run gate 1.\n");
    return gpu ? 0 : 1;
  }

  printf("\n== gate 1: load + execute %s ==\n", argv[1]);
  auto container = zdl::DlContainer::IDlContainer::open(std::string(argv[1]));
  if (!container) {
    printf("open() FAILED: %s\n", zdl::DlSystem::getLastErrorString());
    return 3;
  }
  printf("container opened ok\n");

  zdl::DlSystem::RuntimeList rl;
  rl.add(gpu ? RT::GPU : RT::CPU);
  printf("using runtime   : %s\n", gpu ? "GPU" : "CPU (fallback)");

  zdl::SNPE::SNPEBuilder builder(container.get());
  auto snpe = builder.setRuntimeProcessorOrder(rl).build();
  if (!snpe) {
    // This is the failure mode where the lib loads but the graph can't be
    // prepared for this GPU -- distinct from the open() failure above.
    printf("build() FAILED (graph prepare): %s\n", zdl::DlSystem::getLastErrorString());
    return 4;
  }
  printf("network built ok\n");

  auto inNames = snpe->getInputTensorNames();
  auto outNames = snpe->getOutputTensorNames();
  if (!inNames || !outNames) {
    printf("could not enumerate tensors: %s\n", zdl::DlSystem::getLastErrorString());
    return 5;
  }

  // Allocate every input as zeros -- we are testing that it runs, not what it says.
  zdl::DlSystem::TensorMap inMap, outMap;
  std::vector<std::unique_ptr<zdl::DlSystem::ITensor>> hold;
  for (size_t i = 0; i < (*inNames).size(); i++) {
    const char *n = (*inNames).at(i);
    auto dims = snpe->getInputDimensions(n);
    if (!dims) { printf("no dims for input %s\n", n); return 6; }
    size_t elems = 1;
    printf("  input  %-20s [", n);
    for (size_t d = 0; d < (*dims).rank(); d++) {
      elems *= (*dims)[d];
      printf("%s%zu", d ? "," : "", (*dims)[d]);
    }
    printf("]  (%zu elems)\n", elems);
    auto t = zdl::SNPE::SNPEFactory::getTensorFactory().createTensor(*dims);
    for (auto it = t->begin(); it != t->end(); ++it) *it = 0.0f;
    inMap.add(n, t.get());
    hold.push_back(std::move(t));
  }

  printf("executing...\n");
  auto t0 = std::chrono::steady_clock::now();
  const bool ok = snpe->execute(inMap, outMap);
  auto t1 = std::chrono::steady_clock::now();
  if (!ok) {
    printf("execute() FAILED: %s\n", zdl::DlSystem::getLastErrorString());
    return 7;
  }
  const double first_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
  printf("first execute   : %.1f ms (includes warmup)\n", first_ms);

  for (size_t i = 0; i < (*outNames).size(); i++) {
    const char *n = (*outNames).at(i);
    auto *t = outMap.getTensor(n);
    if (!t) continue;
    size_t cnt = 0; bool bad = false; float f0 = 0.0f;
    for (auto it = t->cbegin(); it != t->cend(); ++it, ++cnt) {
      const float v = *it;
      if (cnt == 0) f0 = v;
      if (v != v) bad = true;  // NaN
    }
    printf("  output %-20s %zu floats, first=%.5f%s\n", n, cnt, f0,
           bad ? "  ** CONTAINS NaN **" : "");
  }

  // gate 2 preview: rough steady-state timing. 20 Hz needs well under 50 ms.
  const int N = 20;
  t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < N; i++) snpe->execute(inMap, outMap);
  t1 = std::chrono::steady_clock::now();
  printf("steady state    : %.1f ms/inference over %d runs (need <50 ms for 20 Hz)\n",
         std::chrono::duration<double, std::milli>(t1 - t0).count() / N, N);

  printf("\nRESULT: 2.48 runs this .dlc on the device.\n");
  return 0;
}
