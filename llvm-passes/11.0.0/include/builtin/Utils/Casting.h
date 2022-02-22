#ifndef BUILTIN_CASTING_H
#define BUILTIN_CASTING_H

#include <llvm/Support/Casting.h>

#define ifcast(ty, var, val) if (ty *var = llvm::dyn_cast<ty>(val))
#define ifncast(ty, var, val) ty *var = llvm::dyn_cast<ty>(val); if (var == nullptr)
#define foreach(ty, var, arr) for (auto *_I : (arr)) if (ty *var = llvm::cast<ty>(_I))

#endif // BUILTIN_CASTING_H
