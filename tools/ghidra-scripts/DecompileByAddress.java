// Decompile functions by address.
// Usage: -postScript DecompileByAddress.java <outputDir> <addressListFile>
// addressListFile: one entry per line, either "0xADDR" or "0xADDR name"
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;

public class DecompileByAddress extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        int ok = 0, fail = 0, missing = 0;

        DecompInterface di = new DecompInterface();
        di.setOptions(di.getOptions());
        di.openProgram(currentProgram);
        SymbolTable st = currentProgram.getSymbolTable();

        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] parts = line.split("\\s+", 2);
                String addrStr = parts[0];
                String hint = parts.length > 1 ? parts[1] : addrStr;
                Address a = toAddr(addrStr);
                Function f = getFunctionAt(a);
                if (f == null) {
                    // try containing function
                    f = getFunctionContaining(a);
                }
                if (f == null) {
                    println("MISSING " + addrStr + " " + hint);
                    missing++;
                    continue;
                }
                DecompileResults res = di.decompileFunction(f, 180, monitor);
                if (!res.decompileCompleted()) {
                    println("FAIL " + addrStr + " " + hint + " : " + res.getErrorMessage());
                    fail++;
                    continue;
                }
                String c = res.getDecompiledFunction().getC();
                String safe = f.getName().replaceAll("[^A-Za-z0-9_.-]", "_");
                if (safe.length() > 150) safe = safe.substring(0, 150);
                File out = new File(outDir, String.format("%08x_%s.c", f.getEntryPoint().getOffset(), safe));
                try (PrintWriter w = new PrintWriter(out)) {
                    w.println("// " + hint + " @ " + f.getEntryPoint() + " size=" + f.getBody().getNumAddresses());
                    // emit demangled symbol aliases for this address
                    for (Symbol s : st.getSymbols(a)) {
                        w.println("// sym: " + s.getName(true));
                    }
                    w.println(c);
                }
                println("WROTE " + out);
                ok++;
            }
        }
        println("DONE ok=" + ok + " fail=" + fail + " missing=" + missing);
        di.dispose();
    }
}
