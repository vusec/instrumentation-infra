ifndef BUILD_SUFFIX
    $(error BUILD_SUFFIX is undefined)
endif
ifndef LLVM_VERSION
    $(error LLVM_VERSION is undefined)
endif
ifndef SETUP_SCRIPT
    $(error SETUP_SCRIPT is undefined)
endif
ifndef USE_BUILTINS
    $(error USE_BUILTINS is undefined)
endif

CXX           ?= g++
OBJDIR        ?= $(shell $(PKG_CONFIG) llvm-passes-$(BUILD_SUFFIX) --objdir)
PREFIX        ?= $(shell $(PKG_CONFIG) llvm-passes-$(BUILD_SUFFIX) --prefix)
PKG_CONFIG    := python3 $(SETUP_SCRIPT) pkg-config
LLVM_PREFIX    = $(shell $(PKG_CONFIG) llvm-$(LLVM_VERSION) --prefix)
LLVM_CONFIG    = $(LLVM_PREFIX)/bin/llvm-config

OLD_CFLAGS    := $(CFLAGS)
CFLAGS         = -Werror -Wall -std=c++0x -g -O2 -I. \
                 $(shell $(LLVM_CONFIG) --cxxflags | sed s/-I/-isystem/g) \
                 $(OLD_CFLAGS)

OLD_LDFLAGS   := $(LDFLAGS)
LDFLAGS        = -g -shared $(OLD_LDFLAGS)
LDFLAGSGOLD    = -Wl,--no-undefined -L$(LLVM_PREFIX)/lib -l:LLVMgold.so -Wl,--rpath=$(LLVM_PREFIX)/lib
ifeq ($(USE_BUILTINS),true)
    BUILTIN_PREFIX  = $(shell $(PKG_CONFIG) llvm-passes-builtin-$(LLVM_VERSION) --prefix)
    CFLAGS         += $(shell $(PKG_CONFIG) llvm-passes-builtin-$(LLVM_VERSION) --cxxflags)
    LDFLAGS        += -L$(BUILTIN_PREFIX) -Wl,-whole-archive -lpasses-builtin -Wl,-no-whole-archive
    BUILTIN_DEP     = $(BUILTIN_PREFIX)/libpasses-builtin.a
else
    BUILTIN_DEP     =
endif

SUBDIRS  ?=
SRCS     := $(wildcard *.cpp) $(foreach d,$(SUBDIRS),$(wildcard $d/*.cpp))
OBJNAMES := $(patsubst %.cpp,%.o,$(SRCS))
OBJS     := $(addprefix $(OBJDIR)/,$(OBJNAMES))
OBJDEPS  := $(OBJS:.o=.d)
LIBGOLD  := libpasses-gold.so
LIBOPT   := libpasses-opt.so

all: $(OBJDIR)/$(LIBGOLD) $(OBJDIR)/$(LIBOPT)

$(OBJDIR)/$(LIBGOLD): LDFLAGS += $(LDFLAGSGOLD)
$(OBJDIR)/$(LIBGOLD) $(OBJDIR)/$(LIBOPT): $(OBJS) $(BUILTIN_DEP)
	cd $(OBJDIR) && $(CXX) $(OBJNAMES) $(LDFLAGS) -o $(@F)

-include $(OBJDEPS)

$(OBJDIR)/%.o: %.cpp | $(OBJDIR)
	@mkdir -p $(@D)  # needed for subdirs
	$(CXX) -c $(CFLAGS) -MMD -o $@ $<

$(OBJDIR):
	mkdir -p $@

clean:
	rm -rf $(OBJDIR)

install: $(PREFIX)/$(LIBGOLD) $(PREFIX)/$(LIBOPT)

$(PREFIX)/%: $(OBJDIR)/% | $(PREFIX)
	cp $< $@

uninstall:
	rm -f $(PREFIX)/$(LIBGOLD) $(PREFIX)/$(LIBOPT)

$(PREFIX):
	mkdir -p $@
