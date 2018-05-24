OBJDIR  ?= .
CC      := gcc
CFLAGS  := -fPIC -O2 -Wall -Wextra
LDFLAGS :=
LDLIBS  :=
LIB     := libbenchutils.a
SRCS    := rusage.c
OBJS    := $(patsubst %.c,$(OBJDIR)/%.o,$(SRCS))
DEPS    := $(OBJS:%.o=%.d)

.PHONY: all clean install uninstall

all: hello $(OBJDIR)/$(LIB)

$(OBJDIR)/$(LIB): $(OBJS)
	ar rcs $@ $^

$(OBJDIR)/%.o: %.c | $(OBJDIR)
	$(CC) -c -MMD $(CFLAGS) -o $@ $<

-include $(DEPS)

hello: hello.c $(OBJDIR)/$(LIB)
	$(CC) -Wall -o $@ $< -L$(OBJDIR) -Wl,--whole-archive \
	    -l:libbenchutils.a -Wl,--no-whole-archive

clean:
	rm -f $(LIB) $(OBJS) $(DEPS)

install: $(INSTALLDIR)/lib/$(LIB)

$(INSTALLDIR)/lib/%: $(OBJDIR)/% | $(INSTALLDIR)/lib
	cp $< $(@D)

uninstall:
	rm -f $(INSTALLDIR)/lib/$(LIB)

$(OBJDIR) $(INSTALLDIR)/lib:
	mkdir -p $@
