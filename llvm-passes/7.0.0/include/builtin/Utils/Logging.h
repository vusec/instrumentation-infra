#ifndef BUILTIN_LOGGING_H
#define BUILTIN_LOGGING_H

//#include <llvm/Support/raw_ostream.h>
#include <llvm/Support/Debug.h>

#ifdef DEBUG_TYPE
# define LOG_LINE(line) (llvm::dbgs() << "[" << DEBUG_TYPE << "] " << line << '\n')
#else
# define LOG_LINE(line) (llvm::dbgs() << line << '\n')
#endif

#define DEBUG_LINE(line) DEBUG(LOG_LINE(line))

#endif // BUILTIN_LOGGING_H
