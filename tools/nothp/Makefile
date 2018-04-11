CFLAGS := -O3 -Wall
EXE    := nothp
OBJDIR := .

.PHONY: all clean install uninstall

all: $(OBJDIR)/$(EXE)

$(OBJDIR)/%: %.c | $(OBJDIR)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(EXE) *.o

install: $(INSTALLDIR)/bin/$(EXE)

$(INSTALLDIR)/bin/%: $(OBJDIR)/% | $(INSTALLDIR)/bin
	cp $< $(@D)

uninstall:
	rm -f $(INSTALLDIR)/bin/$(EXE)

$(OBJDIR) $(INSTALLDIR)/bin:
	mkdir -p $@
