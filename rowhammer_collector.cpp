#include "instruction.h"
#include "prog.h"
#include "platform.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>

using std::cerr;
using std::cout;
using std::endl;
using std::map;
using std::ofstream;
using std::runtime_error;
using std::set;
using std::string;
using std::vector;

namespace {

// DRAM Bender fixed stride registers.
constexpr int CASR = 0;
constexpr int BASR = 1;
constexpr int RASR = 2;

// General-purpose registers used by this application.
constexpr int CAR = 4;
constexpr int RAR = 6;
constexpr int BAR = 7;
constexpr int HMR_COUNTER_REG = 8;
constexpr int HMR_LIMIT_REG = 9;
constexpr int PATTERN_REG = 12;

constexpr int MAX_PROGRAM_INSTRUCTIONS = 2048;

struct Args {
  string output_dir;
  string dimm_id;
  string run_id;
  string target_id;
  string case_id;
  string temperature_source;
  string voltage_source;
  string operator_name;
  string institution;
  string board_id;
  string bitstream_file;

  int rank = 0;
  int bank_group = 0;
  int bank = 0;
  int banks_per_group = 4;
  vector<int> victim_rows;
  vector<int> aggressor_rows;

  int cache_lines_per_row = 128;
  int cache_line_bytes = 64;
  int column_stride = 8;
  int repetitions = 10;

  int victim_pattern_byte = 0x00;
  int aggressor_pattern_byte = 0xFF;
  uint32_t hammer_count_per_row = 100000;

  // Safe initialization/readout timings, expressed as 1.5 ns DDR command slots
  // on the standard U200 DDR4 DRAM-Bender frontend.
  int nominal_trcd_slots = 9;
  int nominal_trp_slots = 9;
  int write_spacing_slots = 7;
  int read_spacing_slots = 7;
  int write_recovery_slots = 8;
  int read_to_precharge_slots = 8;
  int final_guard_slots = 16;

  // Hammer loop waits use 6 ns fabric cycles, matching DRAM Bender's SMC_SLEEP.
  int hammer_act_to_pre_cycles = 5;
  int hammer_pre_to_act_cycles = 3;
  int hammer_final_guard_cycles = 3;

  double slot_ns = 1.5;
  double fabric_cycle_ns = 6.0;
  double temperature_c = 22.0;
  double voltage_vdd_v = 1.20;

  bool refresh_enabled = true;
  bool verify_before_hammer = true;
  bool write_observations_csv = false;
};

struct ReadStats {
  uint64_t bytes_received = 0;
  uint64_t bit_flips = 0;
};

string now_utc() {
  std::time_t t = std::time(nullptr);
  std::tm tm_value;
  gmtime_r(&t, &tm_value);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm_value);
  return string(buf);
}

string json_escape(const string &value) {
  std::ostringstream out;
  for (char c : value) {
    switch (c) {
      case '\\': out << "\\\\"; break;
      case '"': out << "\\\""; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
              << static_cast<int>(static_cast<unsigned char>(c));
        } else {
          out << c;
        }
    }
  }
  return out.str();
}

string csv_escape(const string &value) {
  bool quote = false;
  for (char c : value) {
    if (c == ',' || c == '"' || c == '\n' || c == '\r') {
      quote = true;
      break;
    }
  }
  if (!quote) return value;
  string out = "\"";
  for (char c : value) out += (c == '"') ? "\"\"" : string(1, c);
  out += "\"";
  return out;
}

string join_ints(const vector<int> &values, const string &sep) {
  std::ostringstream out;
  for (size_t i = 0; i < values.size(); ++i) {
    if (i) out << sep;
    out << values[i];
  }
  return out.str();
}

string hex_byte(int value) {
  std::ostringstream out;
  out << std::uppercase << std::hex << std::setw(2) << std::setfill('0') << (value & 0xff);
  return out.str();
}

bool path_exists(const string &path) {
  struct stat st;
  return stat(path.c_str(), &st) == 0;
}

void mkdir_p(const string &path) {
  if (path.empty() || path == ".") return;
  string current;
  if (path[0] == '/') current = "/";
  std::stringstream ss(path);
  string part;
  while (std::getline(ss, part, '/')) {
    if (part.empty()) continue;
    if (!current.empty() && current.back() != '/') current += '/';
    current += part;
    if (::mkdir(current.c_str(), 0755) != 0 && errno != EEXIST) {
      throw runtime_error("mkdir failed for " + current + ": " + std::strerror(errno));
    }
  }
}

bool parse_bool(const string &value) {
  if (value == "1" || value == "true" || value == "TRUE" || value == "yes") return true;
  if (value == "0" || value == "false" || value == "FALSE" || value == "no") return false;
  throw runtime_error("Invalid boolean: " + value);
}

int parse_int(const string &name, const string &value) {
  try {
    size_t used = 0;
    long long result = std::stoll(value, &used, 0);
    if (used != value.size() || result < std::numeric_limits<int>::min() ||
        result > std::numeric_limits<int>::max()) {
      throw runtime_error("");
    }
    return static_cast<int>(result);
  } catch (...) {
    throw runtime_error("Invalid integer for " + name + ": " + value);
  }
}

uint32_t parse_u32(const string &name, const string &value) {
  try {
    size_t used = 0;
    unsigned long long result = std::stoull(value, &used, 0);
    if (used != value.size() || result == 0 || result > 0xffffffffULL) throw runtime_error("");
    return static_cast<uint32_t>(result);
  } catch (...) {
    throw runtime_error("Invalid positive 32-bit integer for " + name + ": " + value);
  }
}

double parse_double(const string &name, const string &value) {
  try {
    size_t used = 0;
    double result = std::stod(value, &used);
    if (used != value.size()) throw runtime_error("");
    return result;
  } catch (...) {
    throw runtime_error("Invalid number for " + name + ": " + value);
  }
}

vector<int> parse_int_list(const string &name, const string &value) {
  vector<int> result;
  std::stringstream ss(value);
  string token;
  while (std::getline(ss, token, ',')) {
    if (token.empty()) throw runtime_error("Empty item in " + name);
    result.push_back(parse_int(name, token));
  }
  if (result.empty()) throw runtime_error(name + " must contain at least one row");
  return result;
}

map<string, string> parse_cli(int argc, char **argv) {
  map<string, string> result;
  for (int i = 1; i < argc; ++i) {
    string key = argv[i];
    if (key.rfind("--", 0) != 0) throw runtime_error("Unexpected positional argument: " + key);
    if (i + 1 >= argc) throw runtime_error("Missing value after " + key);
    result[key.substr(2)] = argv[++i];
  }
  return result;
}

string require(const map<string, string> &cli, const string &key) {
  auto it = cli.find(key);
  if (it == cli.end() || it->second.empty()) throw runtime_error("Missing required --" + key);
  return it->second;
}

string optional(const map<string, string> &cli, const string &key, const string &fallback) {
  auto it = cli.find(key);
  return it == cli.end() ? fallback : it->second;
}

Args load_args(int argc, char **argv) {
  const map<string, string> cli = parse_cli(argc, argv);
  Args a;
  a.output_dir = require(cli, "output-dir");
  a.dimm_id = require(cli, "dimm-id");
  a.run_id = require(cli, "run-id");
  a.target_id = require(cli, "target-id");
  a.case_id = require(cli, "case-id");

  a.rank = parse_int("rank", require(cli, "rank"));
  a.bank_group = parse_int("bank-group", require(cli, "bank-group"));
  a.bank = parse_int("bank", require(cli, "bank"));
  a.banks_per_group = parse_int("banks-per-group", optional(cli, "banks-per-group", "4"));
  a.victim_rows = parse_int_list("victim-rows", require(cli, "victim-rows"));
  a.aggressor_rows = parse_int_list("aggressor-rows", require(cli, "aggressor-rows"));

  a.victim_pattern_byte = parse_int("victim-pattern", require(cli, "victim-pattern"));
  a.aggressor_pattern_byte = parse_int("aggressor-pattern", require(cli, "aggressor-pattern"));
  a.hammer_count_per_row = parse_u32("hammer-count-per-row", require(cli, "hammer-count-per-row"));

  a.cache_lines_per_row = parse_int("cache-lines-per-row", optional(cli, "cache-lines-per-row", "128"));
  a.cache_line_bytes = parse_int("cache-line-bytes", optional(cli, "cache-line-bytes", "64"));
  a.column_stride = parse_int("column-stride", optional(cli, "column-stride", "8"));
  a.repetitions = parse_int("repetitions", optional(cli, "repetitions", "10"));

  a.nominal_trcd_slots = parse_int("nominal-trcd-slots", require(cli, "nominal-trcd-slots"));
  a.nominal_trp_slots = parse_int("nominal-trp-slots", require(cli, "nominal-trp-slots"));
  a.write_spacing_slots = parse_int("write-spacing-slots", optional(cli, "write-spacing-slots", "7"));
  a.read_spacing_slots = parse_int("read-spacing-slots", optional(cli, "read-spacing-slots", "7"));
  a.write_recovery_slots = parse_int("write-recovery-slots", optional(cli, "write-recovery-slots", "8"));
  a.read_to_precharge_slots = parse_int("read-to-precharge-slots", optional(cli, "read-to-precharge-slots", "8"));
  a.final_guard_slots = parse_int("final-guard-slots", optional(cli, "final-guard-slots", "16"));

  a.hammer_act_to_pre_cycles = parse_int("hammer-act-to-pre-cycles", optional(cli, "hammer-act-to-pre-cycles", "5"));
  a.hammer_pre_to_act_cycles = parse_int("hammer-pre-to-act-cycles", optional(cli, "hammer-pre-to-act-cycles", "3"));
  a.hammer_final_guard_cycles = parse_int("hammer-final-guard-cycles", optional(cli, "hammer-final-guard-cycles", "3"));

  a.slot_ns = parse_double("slot-ns", optional(cli, "slot-ns", "1.5"));
  a.fabric_cycle_ns = parse_double("fabric-cycle-ns", optional(cli, "fabric-cycle-ns", "6.0"));
  a.temperature_c = parse_double("temperature-c", require(cli, "temperature-c"));
  a.voltage_vdd_v = parse_double("voltage-vdd-v", require(cli, "voltage-vdd-v"));

  a.refresh_enabled = parse_bool(optional(cli, "refresh-enabled", "true"));
  a.verify_before_hammer = parse_bool(optional(cli, "verify-before-hammer", "true"));
  a.write_observations_csv = parse_bool(optional(cli, "write-observations-csv", "false"));

  a.temperature_source = optional(cli, "temperature-source", "manual");
  a.voltage_source = optional(cli, "voltage-source", "manual");
  a.operator_name = optional(cli, "operator", "");
  a.institution = optional(cli, "institution", "");
  a.board_id = optional(cli, "board-id", "ALVEO_U200_01");
  a.bitstream_file = optional(cli, "bitstream-file", "unknown");

  if (a.victim_pattern_byte < 0 || a.victim_pattern_byte > 255 ||
      a.aggressor_pattern_byte < 0 || a.aggressor_pattern_byte > 255) {
    throw runtime_error("victim/aggressor patterns must be byte values 0..255");
  }
  if (a.rank < 0 || a.rank > 3) throw runtime_error("rank must be non-negative and supported by the bitstream");
  if (a.bank_group < 0 || a.bank < 0 || a.banks_per_group <= 0) throw runtime_error("invalid bank coordinates");
  if (a.cache_lines_per_row <= 0 || a.cache_line_bytes != 64 || a.column_stride <= 0) {
    throw runtime_error("this U200 collector requires positive cache_lines_per_row, cache_line_bytes=64, and positive column_stride");
  }
  if (a.repetitions <= 0) throw runtime_error("repetitions must be positive");
  if (a.slot_ns <= 0.0 || a.fabric_cycle_ns <= 0.0) throw runtime_error("cycle metadata must be positive");

  const int timings[] = {a.nominal_trcd_slots, a.nominal_trp_slots, a.write_spacing_slots,
                         a.read_spacing_slots, a.write_recovery_slots,
                         a.read_to_precharge_slots, a.final_guard_slots,
                         a.hammer_act_to_pre_cycles, a.hammer_pre_to_act_cycles,
                         a.hammer_final_guard_cycles};
  for (int v : timings) if (v < 0) throw runtime_error("timing/wait values cannot be negative");

  if (a.aggressor_rows.size() > 128) throw runtime_error("at most 128 aggressor rows are supported per target");
  set<int> victim_set;
  for (int row : a.victim_rows) {
    if (row < 0) throw runtime_error("victim rows must be non-negative");
    if (!victim_set.insert(row).second) throw runtime_error("duplicate victim row: " + std::to_string(row));
  }
  set<int> aggressor_set;
  for (int row : a.aggressor_rows) {
    if (row < 0) throw runtime_error("aggressor rows must be non-negative");
    if (!aggressor_set.insert(row).second) throw runtime_error("duplicate aggressor row: " + std::to_string(row));
    if (victim_set.count(row)) throw runtime_error("a row cannot be both victim and aggressor: " + std::to_string(row));
  }

  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  if (flat_bank < 0 || flat_bank > 15) {
    throw runtime_error("encoded flat bank is outside 0..15; verify bank-group/bank geometry and U200 bitstream");
  }
  return a;
}

class DdrPacker {
 public:
  DdrPacker(Program &program, int rank) : program_(program), rank_(rank) {}

  void emit(Mininst command) {
    slots_.push_back(command);
    if (slots_.size() == 4) flush_full();
  }

  void nops(int count) {
    for (int i = 0; i < count; ++i) emit(SMC_NOP(rank_));
  }

  void flush() {
    while (!slots_.empty() && slots_.size() < 4) slots_.push_back(SMC_NOP(rank_));
    if (slots_.size() == 4) flush_full();
  }

 private:
  void flush_full() {
    program_.add_inst(__pack_mininsts(slots_[0], slots_[1], slots_[2], slots_[3]));
    slots_.clear();
  }

  Program &program_;
  int rank_;
  vector<Mininst> slots_;
};

uint32_t repeated_pattern32(int pattern_byte) {
  const uint32_t b = static_cast<uint32_t>(pattern_byte & 0xff);
  return b | (b << 8) | (b << 16) | (b << 24);
}

void add_wait_cycles(Program &program, int cycles, int rank) {
  if (cycles <= 0) return;
  if (cycles >= 3) {
    program.add_inst(SMC_SLEEP(static_cast<uint32_t>(cycles)));
    return;
  }
  for (int i = 0; i < cycles; ++i) {
    program.add_inst(__pack_mininsts(SMC_NOP(rank), SMC_NOP(rank), SMC_NOP(rank), SMC_NOP(rank)));
  }
}

void add_single_ddr(Program &program, Mininst command, int rank) {
  program.add_inst(__pack_mininsts(command, SMC_NOP(rank), SMC_NOP(rank), SMC_NOP(rank)));
}

int estimate_row_io_program_instructions(const Args &a) {
  const int setup = 1 + 1 + 1 + 1 + 1 + 2 + 1 + 16;
  const int ddr_slots =
      1 + a.nominal_trp_slots + 1 + a.nominal_trcd_slots +
      a.cache_lines_per_row + std::max(0, a.cache_lines_per_row - 1) *
      std::max(a.write_spacing_slots, a.read_spacing_slots) +
      std::max(a.write_recovery_slots, a.read_to_precharge_slots) + 1 + a.final_guard_slots;
  return setup + (ddr_slots + 3) / 4 + 1;
}

Program build_write_row_program(const Args &a, int row, int pattern_byte) {
  if (estimate_row_io_program_instructions(a) >= MAX_PROGRAM_INSTRUCTIONS) {
    throw runtime_error("row write program may exceed the U200 2048-instruction frontend; reduce geometry or spacing");
  }
  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  Program program;
  program.add_inst(SMC_LI(a.column_stride, CASR));
  program.add_inst(SMC_LI(1, BASR));
  program.add_inst(SMC_LI(1, RASR));
  program.add_inst(SMC_LI(flat_bank, BAR));
  program.add_inst(SMC_LI(row, RAR));
  program.add_inst(SMC_LI(0, CAR));

  program.add_inst(SMC_LI(repeated_pattern32(pattern_byte), PATTERN_REG));
  for (int i = 0; i < 16; ++i) program.add_inst(SMC_LDWD(PATTERN_REG, i));

  DdrPacker ddr(program, a.rank);
  ddr.emit(SMC_PRE(BAR, 0, 0, a.rank));
  ddr.nops(a.nominal_trp_slots);
  ddr.emit(SMC_ACT(BAR, 0, RAR, 0, a.rank));
  ddr.nops(a.nominal_trcd_slots);
  for (int cl = 0; cl < a.cache_lines_per_row; ++cl) {
    ddr.emit(SMC_WRITE(BAR, 0, CAR, 1, a.rank, 0));
    if (cl + 1 < a.cache_lines_per_row) ddr.nops(a.write_spacing_slots);
  }
  ddr.nops(a.write_recovery_slots);
  ddr.emit(SMC_PRE(BAR, 0, 0, a.rank));
  ddr.nops(a.final_guard_slots);
  ddr.flush();
  program.add_inst(SMC_END());
  return program;
}

Program build_read_row_program(const Args &a, int row) {
  if (estimate_row_io_program_instructions(a) >= MAX_PROGRAM_INSTRUCTIONS) {
    throw runtime_error("row read program may exceed the U200 2048-instruction frontend; reduce geometry or spacing");
  }
  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  Program program;
  program.add_inst(SMC_LI(a.column_stride, CASR));
  program.add_inst(SMC_LI(1, BASR));
  program.add_inst(SMC_LI(1, RASR));
  program.add_inst(SMC_LI(flat_bank, BAR));
  program.add_inst(SMC_LI(row, RAR));
  program.add_inst(SMC_LI(0, CAR));

  DdrPacker ddr(program, a.rank);
  ddr.emit(SMC_PRE(BAR, 0, 0, a.rank));
  ddr.nops(a.nominal_trp_slots);
  ddr.emit(SMC_ACT(BAR, 0, RAR, 0, a.rank));
  ddr.nops(a.nominal_trcd_slots);
  for (int cl = 0; cl < a.cache_lines_per_row; ++cl) {
    ddr.emit(SMC_READ(BAR, 0, CAR, 1, a.rank, 0));
    if (cl + 1 < a.cache_lines_per_row) ddr.nops(a.read_spacing_slots);
  }
  ddr.nops(a.read_to_precharge_slots);
  ddr.emit(SMC_PRE(BAR, 0, 0, a.rank));
  ddr.nops(a.final_guard_slots);
  ddr.flush();
  program.add_inst(SMC_END());
  return program;
}

Program build_hammer_program(const Args &a) {
  // One loop iteration activates every aggressor row exactly once.
  // Therefore hammer_count_per_row is also the number of loop iterations.
  // The sequence is intentionally explicit so arbitrary aggressor row lists can
  // be configured without relying on host virtual-address mappings.
  const int estimated = 7 + static_cast<int>(a.aggressor_rows.size()) * 5 + 4;
  if (estimated >= MAX_PROGRAM_INSTRUCTIONS) {
    throw runtime_error("hammer program may exceed the U200 2048-instruction frontend; reduce aggressor row count");
  }

  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  Program program;
  program.add_inst(SMC_LI(flat_bank, BAR));
  program.add_inst(SMC_LI(0, HMR_COUNTER_REG));
  program.add_inst(SMC_LI(a.hammer_count_per_row, HMR_LIMIT_REG));

  // Establish a known closed-bank state before the first ACT.
  add_single_ddr(program, SMC_PRE(BAR, 0, 0, a.rank), a.rank);
  add_wait_cycles(program, a.hammer_pre_to_act_cycles, a.rank);

  program.add_label("HMR_BEGIN");
  for (int row : a.aggressor_rows) {
    program.add_inst(SMC_LI(static_cast<uint32_t>(row), RAR));
    add_single_ddr(program, SMC_ACT(BAR, 0, RAR, 0, a.rank), a.rank);
    add_wait_cycles(program, a.hammer_act_to_pre_cycles, a.rank);
    add_single_ddr(program, SMC_PRE(BAR, 0, 0, a.rank), a.rank);
    add_wait_cycles(program, a.hammer_pre_to_act_cycles, a.rank);
  }

  program.add_inst(SMC_ADDI(HMR_COUNTER_REG, 1, HMR_COUNTER_REG));
  program.add_branch(Program::BR_TYPE::BL, HMR_COUNTER_REG, HMR_LIMIT_REG, "HMR_BEGIN");
  add_wait_cycles(program, a.hammer_final_guard_cycles, a.rank);
  program.add_inst(SMC_END());
  return program;
}

string rep_name(int repetition) {
  std::ostringstream out;
  out << "REP_" << std::setw(2) << std::setfill('0') << repetition;
  return out.str();
}

uint64_t total_hammer_activations(const Args &a) {
  return static_cast<uint64_t>(a.hammer_count_per_row) *
         static_cast<uint64_t>(a.aggressor_rows.size());
}

void write_hammer_sequence(const Args &a, const string &rep_dir) {
  ofstream out((rep_dir + "/hammer_sequence.csv").c_str(), std::ios::trunc);
  if (!out) throw runtime_error("Unable to open hammer_sequence.csv");
  out << "sequence_index,rank,bank_group,bank,flat_bank,aggressor_row,aggressor_pattern_hex,activations_requested\n";
  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  for (size_t i = 0; i < a.aggressor_rows.size(); ++i) {
    out << i << ',' << a.rank << ',' << a.bank_group << ',' << a.bank << ',' << flat_bank << ','
        << a.aggressor_rows[i] << ',' << hex_byte(a.aggressor_pattern_byte) << ','
        << a.hammer_count_per_row << '\n';
  }
}

void write_trial_json(const Args &a, const string &rep_dir, int repetition,
                      const string &status, uint64_t baseline_flips,
                      uint64_t post_flips, double hammer_elapsed_seconds,
                      const string &error_message, const string &started_at,
                      const string &finished_at) {
  const string temp_path = rep_dir + "/trial.json.tmp";
  const string final_path = rep_dir + "/trial.json";
  ofstream out(temp_path.c_str(), std::ios::trunc);
  if (!out) return;

  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  out << "{\n";
  out << "  \"schema_version\": 1,\n";
  out << "  \"status\": \"" << json_escape(status) << "\",\n";
  out << "  \"started_at_utc\": \"" << json_escape(started_at) << "\",\n";
  out << "  \"finished_at_utc\": \"" << json_escape(finished_at) << "\",\n";
  out << "  \"dimm_id\": \"" << json_escape(a.dimm_id) << "\",\n";
  out << "  \"run_id\": \"" << json_escape(a.run_id) << "\",\n";
  out << "  \"target_id\": \"" << json_escape(a.target_id) << "\",\n";
  out << "  \"case_id\": \"" << json_escape(a.case_id) << "\",\n";
  out << "  \"repetition\": " << repetition << ",\n";
  out << "  \"rank\": " << a.rank << ",\n";
  out << "  \"bank_group\": " << a.bank_group << ",\n";
  out << "  \"bank\": " << a.bank << ",\n";
  out << "  \"flat_bank\": " << flat_bank << ",\n";

  out << "  \"victim_rows\": [";
  for (size_t i = 0; i < a.victim_rows.size(); ++i) {
    if (i) out << ", ";
    out << a.victim_rows[i];
  }
  out << "],\n";
  out << "  \"aggressor_rows\": [";
  for (size_t i = 0; i < a.aggressor_rows.size(); ++i) {
    if (i) out << ", ";
    out << a.aggressor_rows[i];
  }
  out << "],\n";

  out << "  \"victim_pattern_hex\": \"" << hex_byte(a.victim_pattern_byte) << "\",\n";
  out << "  \"aggressor_pattern_hex\": \"" << hex_byte(a.aggressor_pattern_byte) << "\",\n";
  out << "  \"hammer_count_per_row\": " << a.hammer_count_per_row << ",\n";
  out << "  \"total_hammer_activations\": " << total_hammer_activations(a) << ",\n";
  out << "  \"hammer_order\": \"configured_order_round_robin\",\n";
  out << "  \"hammer_act_to_pre_cycles\": " << a.hammer_act_to_pre_cycles << ",\n";
  out << "  \"hammer_pre_to_act_cycles\": " << a.hammer_pre_to_act_cycles << ",\n";
  out << "  \"hammer_final_guard_cycles\": " << a.hammer_final_guard_cycles << ",\n";
  out << "  \"fabric_cycle_ns_metadata\": " << std::setprecision(10) << a.fabric_cycle_ns << ",\n";
  out << "  \"nominal_trcd_slots\": " << a.nominal_trcd_slots << ",\n";
  out << "  \"nominal_trp_slots\": " << a.nominal_trp_slots << ",\n";
  out << "  \"slot_ns_metadata\": " << std::setprecision(10) << a.slot_ns << ",\n";
  out << "  \"refresh_enabled\": " << (a.refresh_enabled ? "true" : "false") << ",\n";
  out << "  \"verify_before_hammer\": " << (a.verify_before_hammer ? "true" : "false") << ",\n";
  out << "  \"temperature_c\": " << std::setprecision(10) << a.temperature_c << ",\n";
  out << "  \"temperature_source\": \"" << json_escape(a.temperature_source) << "\",\n";
  out << "  \"voltage_vdd_v\": " << std::setprecision(10) << a.voltage_vdd_v << ",\n";
  out << "  \"voltage_source\": \"" << json_escape(a.voltage_source) << "\",\n";
  out << "  \"baseline_flip_count\": " << baseline_flips << ",\n";
  out << "  \"post_hammer_flip_count\": " << post_flips << ",\n";
  out << "  \"hammer_elapsed_seconds_host\": " << std::setprecision(10) << hammer_elapsed_seconds << ",\n";
  out << "  \"reads_file\": \"reads.bin\",\n";
  out << "  \"read_index_file\": \"read_index.csv\",\n";
  out << "  \"flips_file\": \"flips.csv\",\n";
  out << "  \"observations_file\": "
      << (a.write_observations_csv ? "\"observations.csv\"" : "null") << ",\n";
  out << "  \"baseline_reads_file\": "
      << (a.verify_before_hammer ? "\"baseline_reads.bin\"" : "null") << ",\n";
  out << "  \"board_id\": \"" << json_escape(a.board_id) << "\",\n";
  out << "  \"bitstream_file\": \"" << json_escape(a.bitstream_file) << "\",\n";
  out << "  \"operator\": \"" << json_escape(a.operator_name) << "\",\n";
  out << "  \"institution\": \"" << json_escape(a.institution) << "\",\n";
  out << "  \"error\": \"" << json_escape(error_message) << "\"\n";
  out << "}\n";
  out.close();
  std::rename(temp_path.c_str(), final_path.c_str());
}

string common_csv_header() {
  return "dimm_id,run_id,target_id,case_id,phase,victim_pattern_hex,aggressor_pattern_hex,"
         "hammer_count_per_row,total_hammer_activations,aggressor_rows,repetition,temperature_c,"
         "temperature_source,voltage_vdd_v,voltage_source,refresh_enabled,rank,bank_group,bank,flat_bank,"
         "row,cache_line,column,byte_in_cache_line,bit_in_byte,bit_index_in_cache_line,bit_index_in_row,"
         "raw_byte_offset,expected_bit,observed_bit,did_flip,flip_direction\n";
}

void write_bit_row(ofstream &out, const Args &a, const string &phase, int repetition,
                   int row, int cache_line, int byte_in_cache_line, int bit_in_byte,
                   uint64_t raw_byte_offset, int expected_bit, int observed_bit) {
  const bool flipped = expected_bit != observed_bit;
  const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
  const int column = cache_line * a.column_stride;
  const int bit_index_in_cache_line = byte_in_cache_line * 8 + bit_in_byte;
  const uint64_t row_byte_offset = static_cast<uint64_t>(cache_line) * a.cache_line_bytes + byte_in_cache_line;
  const uint64_t bit_index_in_row = row_byte_offset * 8 + bit_in_byte;
  string direction;
  if (flipped) direction = expected_bit == 0 ? "0_to_1" : "1_to_0";

  out << csv_escape(a.dimm_id) << ',' << csv_escape(a.run_id) << ',' << csv_escape(a.target_id) << ','
      << csv_escape(a.case_id) << ',' << csv_escape(phase) << ',' << hex_byte(a.victim_pattern_byte) << ','
      << hex_byte(a.aggressor_pattern_byte) << ',' << a.hammer_count_per_row << ','
      << total_hammer_activations(a) << ',' << csv_escape(join_ints(a.aggressor_rows, "|")) << ','
      << repetition << ',' << std::setprecision(10) << a.temperature_c << ','
      << csv_escape(a.temperature_source) << ',' << std::setprecision(10) << a.voltage_vdd_v << ','
      << csv_escape(a.voltage_source) << ',' << (a.refresh_enabled ? 1 : 0) << ','
      << a.rank << ',' << a.bank_group << ',' << a.bank << ',' << flat_bank << ','
      << row << ',' << cache_line << ',' << column << ',' << byte_in_cache_line << ',' << bit_in_byte << ','
      << bit_index_in_cache_line << ',' << bit_index_in_row << ',' << raw_byte_offset << ','
      << expected_bit << ',' << observed_bit << ',' << (flipped ? 1 : 0) << ',' << direction << '\n';
}

ReadStats read_victim_rows(SoftMCPlatform &platform, const Args &a, int repetition,
                           const string &rep_dir, const string &phase, const string &prefix) {
  const string stem = prefix.empty() ? "" : prefix + "_";
  ofstream raw((rep_dir + "/" + stem + "reads.bin").c_str(), std::ios::binary | std::ios::trunc);
  ofstream index((rep_dir + "/" + stem + "read_index.csv").c_str(), std::ios::trunc);
  ofstream flips((rep_dir + "/" + stem + "flips.csv").c_str(), std::ios::trunc);
  ofstream observations;
  if (a.write_observations_csv) {
    observations.open((rep_dir + "/" + stem + "observations.csv").c_str(), std::ios::trunc);
  }
  if (!raw || !index || !flips || (a.write_observations_csv && !observations)) {
    throw runtime_error("Unable to open one or more readout files in " + rep_dir);
  }

  index << "dimm_id,run_id,target_id,case_id,phase,victim_pattern_hex,aggressor_pattern_hex,"
           "hammer_count_per_row,total_hammer_activations,repetition,temperature_c,voltage_vdd_v,"
           "refresh_enabled,rank,bank_group,bank,flat_bank,row,cache_line,column,raw_byte_offset,length_bytes\n";
  flips << common_csv_header();
  if (a.write_observations_csv) observations << common_csv_header();

  const size_t row_bytes = static_cast<size_t>(a.cache_lines_per_row) *
                           static_cast<size_t>(a.cache_line_bytes);
  vector<uint8_t> buffer(row_bytes, 0);
  const uint8_t expected_byte = static_cast<uint8_t>(a.victim_pattern_byte);
  ReadStats stats;

  for (int row : a.victim_rows) {
    Program program = build_read_row_program(a, row);
    platform.execute(program);
    const int received = platform.receiveData(buffer.data(), static_cast<int>(row_bytes));
    if (received != static_cast<int>(row_bytes)) {
      std::ostringstream msg;
      msg << "receiveData returned " << received << " bytes; expected " << row_bytes
          << " while reading victim row " << row;
      throw runtime_error(msg.str());
    }

    const uint64_t row_base_offset = stats.bytes_received;
    raw.write(reinterpret_cast<const char *>(buffer.data()), static_cast<std::streamsize>(row_bytes));
    if (!raw) throw runtime_error("failed while writing " + stem + "reads.bin");

    const int flat_bank = a.bank_group * a.banks_per_group + a.bank;
    for (int cl = 0; cl < a.cache_lines_per_row; ++cl) {
      const uint64_t cache_line_offset = row_base_offset +
          static_cast<uint64_t>(cl) * static_cast<uint64_t>(a.cache_line_bytes);
      index << csv_escape(a.dimm_id) << ',' << csv_escape(a.run_id) << ',' << csv_escape(a.target_id) << ','
            << csv_escape(a.case_id) << ',' << csv_escape(phase) << ',' << hex_byte(a.victim_pattern_byte) << ','
            << hex_byte(a.aggressor_pattern_byte) << ',' << a.hammer_count_per_row << ','
            << total_hammer_activations(a) << ',' << repetition << ','
            << std::setprecision(10) << a.temperature_c << ',' << a.voltage_vdd_v << ','
            << (a.refresh_enabled ? 1 : 0) << ',' << a.rank << ',' << a.bank_group << ',' << a.bank << ','
            << flat_bank << ',' << row << ',' << cl << ',' << cl * a.column_stride << ','
            << cache_line_offset << ',' << a.cache_line_bytes << '\n';

      for (int byte_index = 0; byte_index < a.cache_line_bytes; ++byte_index) {
        const size_t row_offset = static_cast<size_t>(cl) * a.cache_line_bytes + byte_index;
        const uint8_t observed_byte = buffer[row_offset];
        const uint64_t raw_byte_offset = row_base_offset + row_offset;
        for (int bit = 0; bit < 8; ++bit) {
          const int expected_bit = (expected_byte >> bit) & 1;
          const int observed_bit = (observed_byte >> bit) & 1;
          if (a.write_observations_csv) {
            write_bit_row(observations, a, phase, repetition, row, cl, byte_index, bit,
                          raw_byte_offset, expected_bit, observed_bit);
          }
          if (expected_bit != observed_bit) {
            ++stats.bit_flips;
            write_bit_row(flips, a, phase, repetition, row, cl, byte_index, bit,
                          raw_byte_offset, expected_bit, observed_bit);
          }
        }
      }
    }

    stats.bytes_received += row_bytes;
    raw.flush();
    index.flush();
    flips.flush();
    if (a.write_observations_csv) observations.flush();
  }

  return stats;
}

void initialize_rows(SoftMCPlatform &platform, const Args &a) {
  for (int row : a.victim_rows) {
    Program p = build_write_row_program(a, row, a.victim_pattern_byte);
    platform.execute(p);
  }
  for (int row : a.aggressor_rows) {
    Program p = build_write_row_program(a, row, a.aggressor_pattern_byte);
    platform.execute(p);
  }
}

void collect_repetition(SoftMCPlatform &platform, const Args &a, int repetition) {
  const string rep_dir = a.output_dir + "/" + rep_name(repetition);
  mkdir_p(rep_dir);
  const string complete_marker = rep_dir + "/.complete";
  if (path_exists(complete_marker)) {
    cout << "Skipping completed " << rep_name(repetition) << endl;
    return;
  }

  const string started_at = now_utc();
  uint64_t baseline_flips = 0;
  uint64_t post_flips = 0;
  double hammer_elapsed = 0.0;
  write_trial_json(a, rep_dir, repetition, "running", baseline_flips, post_flips,
                   hammer_elapsed, "", started_at, "");
  write_hammer_sequence(a, rep_dir);

  try {
    initialize_rows(platform, a);

    if (a.verify_before_hammer) {
      const ReadStats baseline = read_victim_rows(platform, a, repetition, rep_dir, "baseline", "baseline");
      baseline_flips = baseline.bit_flips;
      write_trial_json(a, rep_dir, repetition, "running", baseline_flips, post_flips,
                       hammer_elapsed, "", started_at, "");
    }

    Program hammer = build_hammer_program(a);
    const auto hammer_start = std::chrono::steady_clock::now();
    platform.execute(hammer);
    const auto hammer_end = std::chrono::steady_clock::now();
    hammer_elapsed = std::chrono::duration<double>(hammer_end - hammer_start).count();

    const ReadStats post = read_victim_rows(platform, a, repetition, rep_dir, "post_hammer", "");
    post_flips = post.bit_flips;

    write_trial_json(a, rep_dir, repetition, "complete", baseline_flips, post_flips,
                     hammer_elapsed, "", started_at, now_utc());
    ofstream marker(complete_marker.c_str(), std::ios::trunc);
    marker << now_utc() << '\n';
    marker.close();

    cout << rep_name(repetition) << " complete: baseline_flips=" << baseline_flips
         << " post_hammer_flips=" << post_flips
         << " total_activations=" << total_hammer_activations(a) << endl;
  } catch (const std::exception &e) {
    write_trial_json(a, rep_dir, repetition, "failed", baseline_flips, post_flips,
                     hammer_elapsed, e.what(), started_at, now_utc());
    throw;
  }
}

void print_usage() {
  cerr << "rowhammer_collector: configurable DRAM-Bender RowHammer characterization collector\n"
       << "Required arguments are normally supplied by run_experiment.py.\n"
       << "See config.example.json and README.md.\n";
}

}  // namespace

int main(int argc, char **argv) {
  if (argc == 1) {
    print_usage();
    return 2;
  }

  try {
    const Args args = load_args(argc, argv);
    mkdir_p(args.output_dir);

    const int flat_bank = args.bank_group * args.banks_per_group + args.bank;
    cout << "Starting RowHammer characterization"
         << " DIMM=" << args.dimm_id
         << " target=" << args.target_id
         << " case=" << args.case_id
         << " rank=" << args.rank
         << " BG=" << args.bank_group
         << " bank=" << args.bank
         << " flat_bank=" << flat_bank
         << " victim_rows=" << join_ints(args.victim_rows, ",")
         << " aggressor_rows=" << join_ints(args.aggressor_rows, ",")
         << " hammer_count_per_row=" << args.hammer_count_per_row
         << " refresh=" << (args.refresh_enabled ? "on" : "off")
         << endl;

    SoftMCPlatform platform;
    const int err = platform.init();
    if (err != SOFTMC_SUCCESS) {
      throw runtime_error("Could not initialize SoftMCPlatform; error code " + std::to_string(err));
    }

    platform.reset_fpga();
    platform.set_aref(args.refresh_enabled);

    for (int repetition = 0; repetition < args.repetitions; ++repetition) {
      collect_repetition(platform, args, repetition);
    }

    cout << "Characterization point complete." << endl;
    return 0;
  } catch (const std::exception &e) {
    cerr << "ERROR: " << e.what() << endl;
    return 1;
  }
}
