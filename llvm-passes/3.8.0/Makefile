ifndef OBJDIR
    $(error OBJDIR is undefined)
endif
ifndef PREFIX
    $(error PREFIX is undefined)
endif

CC          = g++
CFLAGS      = -I. -Werror -Wall -std=c++0x -g -O2 `llvm-config --cxxflags`
LDFLAGS     = -g -shared
LLVMLIB     = `llvm-config --prefix`/lib
GOLDLDFLAGS = -Wl,--no-undefined -L$(LLVMLIB) \
              -l:LLVMgold.so -Wl,--rpath=$(LLVMLIB)

SUBDIRS   := utils
SRCS      := $(wildcard *.cpp) $(foreach d,$(SUBDIRS),$(wildcard $d/*.cpp))
OBJNAMES  := $(patsubst %.cpp,%.o,$(SRCS))
OBJS      := $(addprefix $(OBJDIR)/,$(OBJNAMES))
DEPS      := $(OBJS:.o=.d)
LIBGOLD   := libpasses-gold.so
LIBOPT    := libpasses-opt.so
LIBSTATIC := libpasses-builtin.a

all: $(OBJDIR)/$(LIBGOLD) $(OBJDIR)/$(LIBOPT)

$(OBJDIR)/$(LIBGOLD): $(OBJDIR)/$(LIBSTATIC)
	$(CC) $^ $(LDFLAGS) $(GOLDLDFLAGS) -o $@

$(OBJDIR)/$(LIBOPT): $(OBJDIR)/$(LIBSTATIC)
	$(CC) $^ $(LDFLAGS) -o $@

$(OBJDIR)/$(LIBSTATIC): $(OBJS) | $(OBJDIR)
	cd $(OBJDIR) && $(AR) crsv $(@F) $(OBJNAMES)

-include $(DEPS)

$(OBJDIR)/%.o: %.cpp | $(OBJDIR)
	@mkdir -p $(@D)  # needed for subdirs
	$(CC) -c $(CFLAGS) -MMD -o $@ $<

$(OBJDIR):
	mkdir -p $@

clean:
	rm -rf $(OBJDIR)

install: $(addprefix $(PREFIX)/,$(LIBSTATIC) $(LIBGOLD) $(LIBOPT))

$(PREFIX)/%: $(OBJDIR)/% | $(PREFIX)
	cp $< $@

uninstall:
	rm -f $(addprefix $(PREFIX)/,$(LIBSTATIC) $(LIBGOLD) $(LIBOPT))

$(PREFIX):
	mkdir -p $@
