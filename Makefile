program_NAME := rowhammer_collector
program_CXX_SRCS := rowhammer_collector.cpp $(wildcard ../../api/*.c) $(wildcard ../../api/*.cpp)
program_CXX_OBJS := ${program_CXX_SRCS:.cpp=.o}
program_CXX_OBJS := ${program_CXX_OBJS:.c=.o}
program_OBJS := $(program_CXX_OBJS)

program_INCLUDE_DIRS := ../../api ../../../boost-lib
program_LIBRARY_DIRS :=
program_LIBRARIES :=

CPPFLAGS += -g -std=c++11 -pthread -O3 -Wall -Wextra
CPPFLAGS += $(foreach includedir,$(program_INCLUDE_DIRS),-I$(includedir))
LDFLAGS += $(foreach librarydir,$(program_LIBRARY_DIRS),-L$(librarydir))
LDFLAGS += $(foreach library,$(program_LIBRARIES),-l$(library))

CC := g++
CXX := g++

.PHONY: all clean distclean

all: $(program_NAME)

$(program_NAME): $(program_OBJS)
	$(CXX) $(CPPFLAGS) $(program_OBJS) $(LDFLAGS) -o $(program_NAME)

clean:
	@- $(RM) $(program_NAME)
	@- $(RM) $(program_OBJS)

distclean: clean
