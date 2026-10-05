PROGS = popmonitor
CORE  = ../../core
SRCS  = popmonitor.c $(CORE)/src/warp.c $(CORE)/src/features.c $(CORE)/src/detector.c \
        $(CORE)/src/faults.c $(CORE)/src/score.c $(CORE)/src/runs.c
OBJS  = $(SRCS:.c=.o)

PKGS = glib-2.0 gio-2.0 vdostream axparameter
CFLAGS += -O2 -Wall -I$(CORE)/include
CFLAGS += $(shell PKG_CONFIG_PATH=$(PKG_CONFIG_PATH) pkg-config --cflags $(PKGS))
LDLIBS += $(shell PKG_CONFIG_PATH=$(PKG_CONFIG_PATH) pkg-config --libs $(PKGS)) -lm

all: $(PROGS)

$(PROGS): $(OBJS)
	$(CC) $(LDFLAGS) $^ $(LIBS) $(LDLIBS) -o $@

clean:
	rm -f $(PROGS) $(OBJS) *.eap* *_LICENSE.txt package.conf* param.conf tmp*
